"""
Tool schemas + registry for the ReAct graph (visualization) agent.

The agent authors and edits VizSpecs (backend/viz/) — the same specs the
Visualize-tab UI edits, so agent and human have identical capability by
construction. Every spec passes normalize/validate before it touches anything;
validation failures come back as structured observations the loop can
self-correct from.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union, get_args

from pydantic import ValidationError

from database import viz_store
from database.connection import SessionLocal
from helpers.sse import SseEventType
from viz.executor import SpecValidationError, materialize_spec, run_spec
from viz.spec import (
    Aggregate,
    ColorMode,
    FieldType,
    MarkType,
    Predicate,
    TexturePattern,
    TimeUnit,
    VizSpec,
)

logger = logging.getLogger(__name__)


def _literals(literal_type) -> str:
    """Join a Literal type's values with '|' — keeps SPEC_GUIDE enumerations
    in lockstep with viz/spec.py without hand-restating them."""
    return "|".join(get_args(literal_type))


# Compact VizSpec authoring guide — shared by the agent system prompt so every
# LLM surface writes the same schema. Literal enumerations are injected from
# viz.spec at import time (sentinel tokens below); only prose rules are
# maintained by hand.
SPEC_GUIDE = """VizSpec format (JSON):
{
  "title": "Chart title",
  "transform": [
    {"op":"calculate","as":"<new_field>","expr":"(revenue - cost) / revenue"},
    {"op":"filter","field":"<col>","predicate":"<PREDICATES>","value":...},
    {"op":"filter_any","clauses":[[{"op":"filter","field":"region","predicate":"eq","value":"EU"}],[{"op":"filter","field":"region","predicate":"eq","value":"NA"}]]}
  ],
  "mark": {"type":"<MARKS>","stacked":false,"smooth":false,"point":false},
  "encoding": {
    "x":     {"field":"<col>","type":"<TYPES>","timeUnit":"<TIME_UNITS>","bin":{"maxbins":10},"sort":"asc|desc" OR {"encoding":"y","order":"desc"},"topK":{"n":10,"others":true}},
    "y":     {"field":"<col>","type":"quantitative","aggregate":"<AGGREGATES>"},
    "color": {"field":"<col>","type":"nominal"},
    "size":  {"field":"<col>","type":"quantitative","aggregate":"sum"}
  },
  "config": {
    "legend":true,"grid":true,"labels":false,"labelFontSize":12,"labelColor":"#111827",
    "color":{"mode":"<COLOR_MODES>","value":"#2563EB",
      "binding":{"channel":"x|y|color","field":"<col>"},
      "palette":["#2563EB","#0FB981"],
      "assignments":[{"value":"<category>","color":"#DC2626"}]}
  }
}
Rules:
- A channel WITH "aggregate" is a measure; channels WITHOUT it are the grouping dimensions.
- "aggregate":"count" needs no field. Every other aggregate needs a numeric field.
- timeUnit only on temporal fields; bin only on numeric fields; topK only on categorical dimensions.
- sort: a plain "asc"/"desc" orders a channel by its OWN values (e.g. alphabetical). To sort a
  bar/column chart by its measure (largest bar first), put sort on the DIMENSION channel as
  {"encoding":"<measure channel>","order":"desc"} — NOT "desc", which would sort by the label.
- bar/line/area: dimension on x, measure on y; optional color dimension → one series per value.
- scatter: numeric measures on x and y WITH aggregates by default; optional color dimension groups
  the points. Use numeric x and y WITHOUT aggregates only when the user explicitly requests raw
  row-level points. Both axes (and size, when present) must use the same aggregation mode.
  pie: one dimension + one measure; histogram: binned numeric x (count is implicit);
  heatmap: dimensions on x AND y, measure on color.
- config.color.mode separates color meanings: "single" requires value; "categorical" requires a
  grouping-field binding and may assign stable colors to individual values; "continuous" binds to
  the quantitative color channel and uses palette as its low-to-high gradient. Palettes accept
  1–12 "#RRGGBB" colors. Omit color for Stych defaults. config.palette is legacy-only.
- config.labelFontSize accepts 8–32 and sizes visible mark labels across supported chart types.
- config.labelColor accepts a "#RRGGBB" color for visible mark labels. Omit it to use automatic
  contrast, which is recommended for heatmaps with light and dark cells.
- config.texture is {"enabled":true,"pattern":"<TEXTURES>"}.
  Texture is off by default and combines with the selected colors.
- Filters run BEFORE aggregation. Omit anything you don't need — omitted keys get defaults.
- Aggregation runs over the FULL dataset server-side; never fetch data yourself.
Calculated fields ({"op":"calculate"}): derive row-level columns usable in ANY channel or
filter exactly like real columns (e.g. y: {"field":"margin","aggregate":"mean"}). They are
computed before filters. Expression language: fields by name (or [bracketed name] for spaces);
literals 1.5, 'text', true, null; arithmetic + - * / (division is null-safe: x/0 → null);
comparisons = != > >= < <=; logic and/or/not; functions if(cond,a,b), coalesce(a,b,...),
abs(x), round(x[,digits]), floor(x), ceil(x), lower(s), upper(s), length(s). Boolean results
become 'true'/'false' text. A calculation may reference earlier calculated fields.
Use them for rates, ratios, margins, and conditional buckets instead of asking for new data"""

SPEC_GUIDE = (
    SPEC_GUIDE.replace("<MARKS>", _literals(MarkType))
    .replace("<TYPES>", _literals(FieldType))
    .replace("<TIME_UNITS>", _literals(TimeUnit))
    .replace("<AGGREGATES>", _literals(Aggregate))
    .replace("<PREDICATES>", _literals(Predicate))
    .replace("<COLOR_MODES>", _literals(ColorMode))
    .replace("<TEXTURES>", _literals(TexturePattern))
)

VIZ_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_fields",
            "description": (
                "List the dataset's fields (name, type, dimension/measure role, cardinality, "
                "null %, samples). Call this first if you are unsure about exact field names."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_data",
            "description": (
                "Validate a VizSpec and materialize its data WITHOUT saving. Returns row count "
                "and sample rows. Use to sanity-check a spec before create_visualization."
            ),
            "parameters": {
                "type": "object",
                "properties": {"spec": {"type": "object", "description": "The VizSpec."}},
                "required": ["spec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_visualization",
            "description": (
                "Validate, materialize, and SAVE a new visualization. It appears in the user's "
                "Visualize tab immediately and stays live as the data changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {"type": "object", "description": "The VizSpec."},
                    "title": {"type": "string", "description": "Short human title."},
                },
                "required": ["spec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_visualization",
            "description": (
                "Replace an existing visualization's spec (new version, old versions kept). "
                "Use when the user asks to change an existing chart. Fetch the current spec "
                "with get_visualization first, modify it, and send the FULL new spec."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "viz_id": {"type": "string"},
                    "spec": {"type": "object", "description": "The complete new VizSpec."},
                    "title": {"type": "string"},
                    "expected_version": {
                        "type": "integer",
                        "description": "Version returned by get_visualization.",
                    },
                },
                "required": ["viz_id", "spec", "expected_version"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_visualizations",
            "description": "List this session's saved visualizations (id, title, version).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_visualization",
            "description": "Fetch a saved visualization's current spec by id.",
            "parameters": {
                "type": "object",
                "properties": {"viz_id": {"type": "string"}},
                "required": ["viz_id"],
            },
        },
    },
]


def render_catalog_text(catalog: Any) -> str:
    """Compact field-catalog block for prompts/observations."""
    lines = [f"Dataset: {catalog.row_count} rows. Fields (use EXACT names):"]
    for f in catalog.fields:
        bits = [f.type, f.role]
        if f.cardinality is not None:
            bits.append(f"{f.cardinality} distinct")
        if f.null_pct:
            bits.append(f"{f.null_pct}% null")
        if f.is_id:
            bits.append("ID-like — avoid as a chart axis")
        samples = f", samples={f.samples[:3]}" if f.samples else ""
        lines.append(f"  - {f.name!r} [{', '.join(bits)}]{samples}")
    return "\n".join(lines)


def _format_errors(errors: List[Any]) -> str:
    lines = ["SPEC INVALID — fix these and retry:"]
    lines += [f"  - {e.path}: {e.message}" for e in errors]
    return "\n".join(lines)


def _preview_rows(data: Any, limit: int = 10) -> str:
    cols = [c.name for c in data.columns]
    head = [dict(zip(cols, row)) for row in data.rows[:limit]]
    return (
        f"columns={cols} row_count={data.meta.get('row_count')} "
        f"truncated={data.meta.get('truncated')}\nfirst rows: {json.dumps(head, default=str)}"
    )


class VizReactToolRegistry:
    """Executes the graph agent's viz tools against the viz subsystem + store."""

    TOOL_NAMES = frozenset(t["function"]["name"] for t in VIZ_TOOL_SCHEMAS)

    def __init__(
        self,
        sql_agent: Any = None,
        progress_emitter: Any = None,
        author: str = "agent",
    ) -> None:
        self._sql_agent = sql_agent
        self._progress_emitter = progress_emitter
        self._author = author
        # (viz_id, title) pairs this run — the caller embeds them as ```viz fences.
        self.created: List[Dict[str, str]] = []
        self.updated: List[Dict[str, str]] = []

    async def call(self, tool_name: str, tool_input: Union[str, Dict[str, Any]], state: Any) -> str:
        if tool_name not in self.TOOL_NAMES:
            return f"ERROR: Unknown tool '{tool_name}'. Available: {sorted(self.TOOL_NAMES)}"
        args = self._coerce_args(tool_input)
        try:
            handler = getattr(self, f"_tool_{tool_name}")
            return await handler(args, state)
        except SpecValidationError as exc:
            return _format_errors(exc.errors)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]
            )
            return f"SPEC INVALID — schema errors: {issues}\n\n{SPEC_GUIDE}"
        except Exception as exc:  # noqa: BLE001 — observation, not crash
            logger.error("[viz_tools] %s failed: %s", tool_name, exc, exc_info=True)
            return f"ERROR calling {tool_name}: {exc}"

    @staticmethod
    def _coerce_args(tool_input: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(tool_input, dict):
            args = dict(tool_input)
        else:
            try:
                args = json.loads(tool_input or "{}")
            except Exception:  # noqa: BLE001
                args = {}
        spec = args.get("spec")
        if isinstance(spec, str):
            try:
                args["spec"] = json.loads(spec)
            except Exception:  # noqa: BLE001
                pass
        return args

    @staticmethod
    def _user_id(state: Any) -> Optional[str]:
        return str((state.metadata or {}).get("user_id") or "") or None

    async def _emit_viz_event(
        self,
        state: Any,
        action: str,
        viz_id: str,
        title: str,
        *,
        version: Optional[int] = None,
    ) -> None:
        if self._progress_emitter is None:
            return
        await self._progress_emitter.emit(
            "graph",
            f"Visualization {action}: {title}",
            event_type=SseEventType.VIZ.value,
            action=action,
            viz_id=viz_id,
            title=title,
            session_id=state.session_id,
            version=version,
        )

    # ------------------------------------------------------------------ tools

    async def _tool_list_fields(self, _args: Dict[str, Any], state: Any) -> str:
        from viz.catalog import get_field_catalog  # noqa: PLC0415

        catalog = await get_field_catalog(state, sql_agent=self._sql_agent)
        return render_catalog_text(catalog)

    async def _tool_preview_data(self, args: Dict[str, Any], state: Any) -> str:
        spec = VizSpec.model_validate(args.get("spec") or {})
        data = await run_spec(state, spec, sql_agent=self._sql_agent)
        return f"Spec is valid. {_preview_rows(data)}"

    async def _tool_create_visualization(self, args: Dict[str, Any], state: Any) -> str:
        user_id = self._user_id(state)
        if not user_id:
            return "ERROR: no user is associated with this session; cannot save."
        spec = VizSpec.model_validate(args.get("spec") or {})
        title = str(args.get("title") or spec.title or "Untitled chart")
        if spec.title is None:
            spec.title = title
        normalized, data = await materialize_spec(state, spec, sql_agent=self._sql_agent)
        db = SessionLocal()
        try:
            row = viz_store.create_visualization(
                db,
                session_id=state.session_id,
                app_user_id=user_id,
                spec=normalized.to_json_dict(),
                title=title,
                dataset_ref=normalized.data.source,
                author=self._author,
            )
            db.commit()
            viz_id = row.id
        finally:
            db.close()
        self.created.append({"viz_id": viz_id, "title": title})
        await self._emit_viz_event(state, "created", viz_id, title, version=1)
        return (
            f"Created visualization viz_id={viz_id} title={title!r} (version 1). "
            f"{_preview_rows(data, limit=5)}"
        )

    async def _tool_update_visualization(self, args: Dict[str, Any], state: Any) -> str:
        user_id = self._user_id(state)
        if not user_id:
            return "ERROR: no user is associated with this session; cannot save."
        viz_id = str(args.get("viz_id") or "")
        spec = VizSpec.model_validate(args.get("spec") or {})
        if "expected_version" not in args:
            return (
                "ERROR: expected_version is required. Fetch the visualization first, "
                "then retry using the version you received."
            )
        try:
            expected_version = int(args["expected_version"])
        except (TypeError, ValueError):
            return "ERROR: expected_version must be an integer."
        db = SessionLocal()
        try:
            existing = viz_store.get_visualization(db, viz_id, user_id)
            if existing is None:
                return f"ERROR: no visualization with id {viz_id!r}. Use list_visualizations."
            if existing.session_history_id != state.session_id:
                return f"ERROR: visualization {viz_id!r} belongs to a different session."
            if int(existing.version) != expected_version:
                return (
                    f"CONFLICT: visualization {viz_id!r} is now version {existing.version}, "
                    f"not {expected_version}. Fetch it again and reapply the requested change."
                )
            normalized, data = await materialize_spec(
                state,
                spec,
                sql_agent=self._sql_agent,
                source_hint=existing.dataset_ref,
            )
            title = str(args.get("title") or spec.title or existing.title or "Untitled chart")
            try:
                row = viz_store.update_visualization(
                    db,
                    viz_id,
                    user_id,
                    spec=normalized.to_json_dict(),
                    title=title,
                    dataset_ref=normalized.data.source,
                    author=self._author,
                    expected_version=expected_version,
                )
            except viz_store.VersionConflictError as exc:
                db.rollback()
                return f"CONFLICT: {exc} Fetch it again and reapply the requested change."
            db.commit()
            version = row.version if row is not None else "?"
        finally:
            db.close()
        self.updated.append({"viz_id": viz_id, "title": title})
        await self._emit_viz_event(state, "updated", viz_id, title, version=int(version))
        return (
            f"Updated visualization viz_id={viz_id} to version {version}. "
            f"{_preview_rows(data, limit=5)}"
        )

    async def _tool_list_visualizations(self, _args: Dict[str, Any], state: Any) -> str:
        user_id = self._user_id(state)
        if not user_id:
            return "No saved visualizations (anonymous session)."
        db = SessionLocal()
        try:
            rows = viz_store.list_visualizations(db, state.session_id, user_id)
        finally:
            db.close()
        if not rows:
            return "No saved visualizations in this session yet."
        return "\n".join(
            f"- viz_id={r.id} title={r.title!r} version={r.version} "
            f"source={r.dataset_ref!r} updated={r.updated_at}"
            for r in rows
        )

    async def _tool_get_visualization(self, args: Dict[str, Any], state: Any) -> str:
        user_id = self._user_id(state)
        if not user_id:
            return "ERROR: no user is associated with this session."
        viz_id = str(args.get("viz_id") or "")
        db = SessionLocal()
        try:
            row = viz_store.get_visualization(db, viz_id, user_id)
            if row is None:
                return f"ERROR: no visualization with id {viz_id!r}. Use list_visualizations."
            payload = viz_store.viz_to_dict(row)
        finally:
            db.close()
        return (
            f"viz_id={payload['id']} title={payload['title']!r} version={payload['version']}\n"
            f"spec: {json.dumps(payload['spec'], default=str)}"
        )

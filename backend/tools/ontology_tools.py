"""
Tool schemas + registry for the ontology (semantic layer).

These tools hang off the ORCHESTRATOR rather than a dedicated sub-agent,
because their value is in being reached *before* a question is delegated: if
"revenue" has a certified definition, the orchestrator should answer from it
instead of asking the analysis agent to write a fresh calculation.

The division of authority is deliberate and enforced here, not by prompting:
the agent may propose definitions, and every proposal is uncertified. Only a
human certifies, through the Ontology tab or the chat proposal card. An answer
built on a draft is labelled as one.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import ValidationError

from database import ontology_store
from database.connection import SessionLocal
from ontology.executor import OntologyValidationError, preview_query
from ontology.resolve import QueryDimension
from ontology.spec import OntologySpec

logger = logging.getLogger(__name__)

ONTOLOGY_GUIDE = """Ontology definition format (JSON), used by propose_ontology:
  entity:       {"name":"orders","dataset":"<dataset id from list_ontology>","primaryKey":"order_id",
                 "description":"One customer purchase","synonyms":["sales","purchases"]}
  relationship: {"name":"order_customer","fromEntity":"orders","fromField":"customer_id",
                 "toEntity":"customers","toField":"id","cardinality":"many_to_one"}
  dimension:    {"name":"region","entity":"customers","field":"customer_region",
                 "synonyms":["area"],"valueSynonyms":{"CA":["California"]}}
  metric:       {"name":"revenue","entity":"orders","expr":"order_total - refund_amount",
                 "aggregate":"sum","filterExpr":"status != 'cancelled'",
                 "description":"Net of refunds, excluding cancelled orders"}
Rules:
- Names are lower_snake_case and share ONE namespace across all four kinds.
- A metric's `expr` is a row-level expression; `filterExpr` is a condition scoped to that
  metric alone (it folds into the aggregate, so metrics with different rules stay correct
  in one query). `aggregate` is sum|mean|min|max|count|distinct; only count may omit expr.
- Expression language (same as calculated fields): field names, literals 1.5 'text' true null,
  arithmetic + - * /, comparisons = != > >= < <=, and/or/not, and the functions
  if(cond,a,b), coalesce(...), abs, round, floor, ceil, lower, upper, length.
- A dimension belongs to the entity whose dataset holds its column.
- Add a relationship whenever two entities share a key: it is what lets a metric on one
  entity be sliced by a dimension on another."""


ONTOLOGY_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_ontology",
            "description": (
                "List the session's semantic model: entities, relationships, dimensions and "
                "metrics, each marked certified or draft, plus the datasets available to "
                "define new ones over. Call this FIRST whenever the user asks about a "
                "business quantity (revenue, churn, orders, conversion, average order "
                "value, ...) so you answer from the agreed definition instead of inventing one."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_metric",
            "description": (
                "Compute one or more defined metrics, optionally grouped by defined "
                "dimensions. The backend generates the aggregation deterministically — "
                "prefer this over delegating to the analysis agent for any number the "
                "ontology already defines, so chat, charts and exports agree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Metric names from list_ontology.",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "time_unit": {
                                    "type": "string",
                                    "enum": ["year", "quarter", "month", "week", "day", "hour"],
                                    "description": "Grain; temporal dimensions only.",
                                },
                                "top_k": {
                                    "type": "integer",
                                    "description": "Keep only the top N values.",
                                },
                            },
                            "required": ["name"],
                        },
                        "description": "Dimensions to group by. Omit for a grand total.",
                    },
                },
                "required": ["metrics"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_ontology",
            "description": (
                "Draft or REVISE semantic definitions for the user to review. Use when a "
                "question needs a business quantity or a join that is not defined yet, and "
                "again when the user asks to change a proposal you already made — pass the "
                "same name with new values to revise it, or list it in `remove` to drop it. "
                "Revising only works on drafts: a definition the user has certified is theirs, "
                "and will be refused. Proposals are ALWAYS uncertified and appear as a review "
                "card in chat — you cannot certify them. Propose only what the question needs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {"type": "array", "items": {"type": "object"}},
                    "relationships": {"type": "array", "items": {"type": "object"}},
                    "dimensions": {"type": "array", "items": {"type": "object"}},
                    "metrics": {"type": "array", "items": {"type": "object"}},
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of DRAFT definitions to drop from the proposal.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Why these were proposed — recorded as provenance.",
                    },
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _mark(item: Any) -> str:
    return "certified" if getattr(item, "certified", False) else "DRAFT"


def render_ontology_text(spec: OntologySpec, datasets: List[dict]) -> str:
    """Compact text form of the model — what the LLM reads."""
    lines: List[str] = []

    lines.append("Datasets available:")
    if datasets:
        for d in datasets:
            fields = ", ".join(d.get("fields", [])[:25])
            more = "…" if len(d.get("fields", [])) > 25 else ""
            lines.append(f"  - id={d['id']} ({d.get('filename', '?')}): {fields}{more}")
    else:
        lines.append("  (none loaded)")

    if not spec.entities and not spec.metrics:
        lines.append(
            "\nNo semantic model defined yet. Use propose_ontology to draft one for the user."
        )
        return "\n".join(lines)

    lines.append("\nEntities:")
    for e in spec.entities or []:
        pk = f" pk={e.primary_key}" if e.primary_key else ""
        syn = f" synonyms={e.synonyms}" if e.synonyms else ""
        lines.append(f"  - {e.name} [{_mark(e)}] dataset={e.dataset}{pk}{syn}")

    if spec.relationships:
        lines.append("\nRelationships:")
        for r in spec.relationships:
            lines.append(
                f"  - {r.name} [{_mark(r)}]: {r.from_entity}.{r.from_field} → "
                f"{r.to_entity}.{r.to_field} ({r.cardinality})"
            )

    if spec.dimensions:
        lines.append("\nDimensions (group metrics by these):")
        for d in spec.dimensions:
            syn = f" synonyms={d.synonyms}" if d.synonyms else ""
            vals = f" values={d.value_synonyms}" if d.value_synonyms else ""
            lines.append(f"  - {d.name} [{_mark(d)}]: {d.entity}.{d.field}{syn}{vals}")

    if spec.metrics:
        lines.append("\nMetrics:")
        for m in spec.metrics:
            body = f"{m.aggregate}({m.expr})" if m.expr else f"{m.aggregate}(*)"
            filt = f" where {m.filter_expr}" if m.filter_expr else ""
            desc = f" — {m.description}" if m.description else ""
            lines.append(f"  - {m.name} [{_mark(m)}] on {m.entity}: {body}{filt}{desc}")

    return "\n".join(lines)


def _format_errors(errors: List[Any]) -> str:
    body = "\n".join(f"  - {e.path}: {e.message}" for e in errors[:10])
    return f"INVALID — fix these and retry:\n{body}"


def _render_rows(data: Any, limit: int = 25) -> str:
    cols = [c.name for c in data.columns]
    rows = [dict(zip(cols, row)) for row in data.rows[:limit]]
    more = "" if len(data.rows) <= limit else f" (+{len(data.rows) - limit} more rows)"
    return f"columns={cols}\n{json.dumps(rows, default=str)}{more}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class OntologyToolRegistry:
    """Executes the ontology tools against the store + executor."""

    TOOL_NAMES = frozenset(t["function"]["name"] for t in ONTOLOGY_TOOL_SCHEMAS)

    def __init__(
        self,
        sql_agent: Any = None,
        progress_emitter: Any = None,
        author: str = "agent",
        model: Optional[str] = None,
    ) -> None:
        self._sql_agent = sql_agent
        self._progress_emitter = progress_emitter
        self._author = author
        self._model = model
        # Proposals made this run — the caller embeds them as ```ontology fences.
        self.proposals: List[Dict[str, Any]] = []

    async def call(self, tool_name: str, tool_input: Union[str, Dict[str, Any]], state: Any) -> str:
        if tool_name not in self.TOOL_NAMES:
            return f"ERROR: Unknown tool '{tool_name}'. Available: {sorted(self.TOOL_NAMES)}"
        args = self._coerce_args(tool_input)
        try:
            handler = getattr(self, f"_tool_{tool_name}")
            return await handler(args, state)
        except OntologyValidationError as exc:
            return _format_errors(exc.errors)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]
            )
            return f"DEFINITION INVALID — schema errors: {issues}\n\n{ONTOLOGY_GUIDE}"
        except Exception as exc:  # noqa: BLE001 — observation, not crash
            logger.error("[ontology_tools] %s failed: %s", tool_name, exc, exc_info=True)
            return f"ERROR calling {tool_name}: {exc}"

    @staticmethod
    def _coerce_args(tool_input: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(tool_input, dict):
            return dict(tool_input)
        try:
            return json.loads(tool_input or "{}")
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _user_id(state: Any) -> Optional[str]:
        return str((state.metadata or {}).get("user_id") or "") or None

    @staticmethod
    def _datasets(state: Any) -> List[dict]:
        contexts = []
        primary = getattr(state, "data_context", None)
        if primary is not None:
            contexts.append(primary)
        contexts.extend(getattr(state, "additional_data_contexts", None) or [])
        out = []
        for dc in contexts:
            frame = getattr(dc, "working_dataframe", None)
            out.append(
                {
                    "id": str(dc.file_id),
                    "filename": str(dc.filename),
                    "fields": list(frame.columns) if frame is not None else [],
                }
            )
        return out

    def _load(self, state: Any) -> Tuple[Optional[str], OntologySpec, int]:
        """(ontology_id, spec, version) for this user, seeded if absent."""
        user_id = self._user_id(state)
        if not user_id:
            return None, OntologySpec(), 0
        db = SessionLocal()
        try:
            row = ontology_store.get_or_create(db, user_id)
            return row.id, OntologySpec.model_validate(json.loads(row.spec_json)), int(row.version)
        finally:
            db.close()

    def _save(
        self,
        state: Any,
        spec: OntologySpec,
        ontology_id: str,
        version: int,
        summary: str,
        proposed: List[str],
    ) -> int:
        user_id = self._user_id(state)
        db = SessionLocal()
        try:
            row = ontology_store.save_spec(
                db,
                ontology_id,
                str(user_id),
                spec.to_json_dict(),
                expected_version=version,
                author=self._author,
                summary=summary,
                proposed=proposed,
            )
            return int(row.version)
        finally:
            db.close()

    async def _emit(
        self, state: Any, ontology_id: str, version: int, action: str, proposed: List[str]
    ) -> None:
        if self._progress_emitter is None:
            return
        await self._progress_emitter.emit(
            "ontology",
            f"Semantic model {action}: {', '.join(proposed[:3])}",
            event_type="ontology",
            action=action,
            ontology_id=ontology_id,
            version=version,
            proposed=proposed,
            session_id=state.session_id,
        )

    # ------------------------------------------------------------------ tools

    async def _tool_list_ontology(self, _args: Dict[str, Any], state: Any) -> str:
        _, spec, _ = self._load(state)
        return render_ontology_text(spec, self._datasets(state))

    async def _tool_query_metric(self, args: Dict[str, Any], state: Any) -> str:
        _, spec, _ = self._load(state)
        metrics = [str(m) for m in (args.get("metrics") or [])]
        if not metrics:
            return "ERROR: name at least one metric. Call list_ontology to see what is defined."

        dimensions = []
        for raw in args.get("dimensions") or []:
            if isinstance(raw, str):
                dimensions.append(QueryDimension(name=raw))
            elif isinstance(raw, dict):
                dimensions.append(
                    QueryDimension(
                        name=str(raw.get("name") or ""),
                        time_unit=raw.get("time_unit") or raw.get("timeUnit"),
                        top_k=raw.get("top_k") or raw.get("topK"),
                    )
                )

        resolved, data = await preview_query(
            state, spec, metrics, dimensions, sql_agent=self._sql_agent
        )
        out = _render_rows(data)
        if resolved.spans_entities:
            joined = ", ".join(sorted({s.right_entity for s in resolved.joins}))
            out += f"\n(joined via the ontology's relationships to: {joined})"
        fan_out = resolved.fan_out_relationships
        if fan_out:
            out += (
                "\n\nWARNING: this query joins through "
                f"{', '.join(fan_out)}, which matches MANY rows per row of "
                f"'{resolved.base_entity.name}'. Additive measures (sum, count) are "
                "inflated by the duplication. Say so plainly in your answer, or "
                "re-ask for a metric defined on the other entity instead."
            )
        if resolved.uncertified:
            out += (
                "\n\nWARNING: these definitions are UNCERTIFIED drafts: "
                f"{', '.join(resolved.uncertified)}. Say so plainly in your answer and suggest "
                "the user review them in the Ontology tab. Do not present these as established "
                "figures."
            )
        return out

    async def _tool_propose_ontology(self, args: Dict[str, Any], state: Any) -> str:
        ontology_id, spec, version = self._load(state)
        if not ontology_id:
            return "ERROR: no session user; cannot save proposals."

        intent = str(args.get("intent") or "").strip() or None
        provenance = {"author": "agent", "intent": intent, "model": self._model}

        payload = spec.to_json_dict()
        certified = {
            i.name
            for i in [*spec.entities, *spec.relationships, *spec.dimensions, *spec.metrics]
            if i.certified
        }
        added: List[str] = []
        updated: List[str] = []
        removed: List[str] = []
        refused: List[str] = []

        # Removals first, so a rename expressed as remove-then-add works.
        for name in args.get("remove") or []:
            name = str(name)
            if name in certified:
                refused.append(name)
                continue
            for key in ("entities", "relationships", "dimensions", "metrics"):
                before = payload.get(key) or []
                after = [i for i in before if i.get("name") != name]
                if len(after) != len(before):
                    payload[key] = after
                    removed.append(name)

        for key in ("entities", "relationships", "dimensions", "metrics"):
            incoming = args.get(key) or []
            if not isinstance(incoming, list):
                continue
            for item in incoming:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not name:
                    continue
                # A certified definition is a human's sign-off; revising it is
                # the human's call, not the agent's. A DRAFT is still the
                # agent's working proposal, so a follow-up may revise it —
                # that is what makes "actually, exclude refunds too" work.
                if name in certified:
                    refused.append(str(name))
                    continue
                item = {**item, "certified": False, "provenance": provenance}
                existing = payload.get(key) or []
                at = next((n for n, i in enumerate(existing) if i.get("name") == name), None)
                if at is None:
                    payload.setdefault(key, []).append(item)
                    added.append(str(name))
                else:
                    existing[at] = item
                    updated.append(str(name))

        touched = added + updated + removed
        if not touched:
            if refused:
                return (
                    "Refused: "
                    + ", ".join(sorted(set(refused)))
                    + " are certified. A human has signed these off — ask the user to change "
                    "them in the Ontology tab, or propose a differently-named definition."
                )
            return "Nothing to propose — no definitions were added, changed or removed."

        # Validate the merged model before persisting; a bad proposal must come
        # back as a fixable observation, not land in the user's ontology.
        merged = OntologySpec.model_validate(payload)
        from ontology.executor import catalogs_for_spec as _catalogs  # noqa: PLC0415
        from ontology.validate import validate_spec as _validate  # noqa: PLC0415

        catalogs = await _catalogs(state, merged, self._sql_agent)
        errors = _validate(merged, catalogs)
        if errors:
            return _format_errors(errors) + f"\n\n{ONTOLOGY_GUIDE}"

        parts = []
        if added:
            parts.append(f"added {', '.join(added)}")
        if updated:
            parts.append(f"revised {', '.join(updated)}")
        if removed:
            parts.append(f"removed {', '.join(removed)}")
        summary = "; ".join(parts)

        new_version = self._save(
            state,
            merged,
            ontology_id,
            version,
            summary=f"proposed: {summary}",
            proposed=added + updated,
        )
        self.proposals.append(
            {
                "ontology_id": ontology_id,
                "version": new_version,
                "proposed": added + updated,
            }
        )
        await self._emit(state, ontology_id, new_version, "proposed", added + updated)

        note = ""
        if refused:
            note = (
                f" Left alone (already certified): {', '.join(sorted(set(refused)))} — "
                "tell the user those need changing in the Ontology tab."
            )
        return (
            f"Proposal updated as DRAFTS for the user to review — {summary}. A fresh review "
            "card is now at the bottom of the chat and any earlier card is marked superseded. "
            "Tell the user what changed and why; nothing is certified until they accept." + note
        )

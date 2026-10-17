"""
"Ask this chart" — conversational inquiry anchored to a chart selection.

Deterministic-first, exactly like the rest of the viz subsystem: the selection
is compiled to spec-level predicates, selected-vs-rest stats and a driver
(contribution) analysis are computed by running REAL specs through
``run_spec`` (so both engines are covered by construction), and only then does
ONE LLM call narrate the evidence. The model never sees raw data beyond the
already-capped tidy tables, and anything it proposes back (a drill-down spec)
goes through normal spec validation — with one repair round — before it is
persisted as an ordinary live visualization.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from database import viz_store
from database.connection import SessionLocal

from .catalog import FieldCatalog, get_field_catalog
from .executor import SpecValidationError, materialize_spec, resolve_data_context, run_spec
from .plan import VizData
from .selection import Selection, _clauses, match_rows, selection_stats, selection_to_filters
from .spec import ChannelDef, Encoding, FilterAnyTransform, FilterTransform, MarkDef, VizSpec

logger = logging.getLogger(__name__)

MAX_DRIVER_DIMS = 4
_DRIVER_TOP_SEGMENTS = 6
_PROMPT_ROW_CAP = 60

_MODE_INSTRUCTIONS = {
    "explain": (
        "Explain what characterizes the SELECTED slice and why it might stand out. "
        "Lead with the strongest evidence from the driver analysis."
    ),
    "compare": (
        "Compare the SELECTED slice against the rest of the data: totals, averages, "
        "and where their composition differs most."
    ),
    "drill": (
        "Propose the single most useful drill-down chart into the SELECTED slice, and "
        "briefly say what it will reveal. You MUST include drill_spec."
    ),
    "custom": "Answer the user's question about the SELECTED slice using the evidence.",
}

_SYSTEM_PROMPT = """You are the chart-inquiry engine of a data platform. The user selected part
of a chart and asked about it. You receive deterministic, pre-computed evidence: the chart spec,
the selection, selected-vs-rest statistics, and a driver analysis (how the selection's composition
differs from the overall data across other fields). Ground every claim in that evidence — never
invent numbers.

Respond with ONLY a JSON object:
{
  "headline": "<= 12 words, the single most important finding",
  "insight": "2-5 sentences in plain language. Cite concrete numbers from the evidence.",
  "drill_spec": <a VizSpec JSON filtered to the selection, or null if a drill-down adds nothing>,
  "drill_title": "<short title for the drill-down chart, when drill_spec is set>"
}

{spec_guide}

drill_spec rules: include the provided selection filters in its transform so it shows ONLY the
selected slice; pick a different angle than the original chart (a breakdown field the driver
analysis flags, or a finer timeUnit). Use EXACT field names from the field list."""


def _parse_json_obj(text: str) -> Optional[Dict[str, Any]]:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start:end])
                return parsed if isinstance(parsed, dict) else None
            except Exception:  # noqa: BLE001
                return None
    return None


def describe_selection(spec: VizSpec, selection: Selection) -> str:
    clauses = []
    for clause in _clauses(selection):
        parts: List[str] = []
        for item in clause:
            ch = getattr(spec.encoding, item.channel, None)
            fld = ch.field if ch is not None else item.channel
            if item.values is not None:
                vals = ", ".join(str(v) for v in item.values[:12])
                more = f" (+{len(item.values) - 12} more)" if len(item.values) > 12 else ""
                parts.append(f"{fld} in [{vals}]{more}")
            elif item.range is not None:
                op = "<" if item.range.exclusive_max else "<="
                parts.append(f"{item.range.min} <= {fld} {op} {item.range.max}")
        clauses.append(" AND ".join(parts))
    return " OR ".join(f"({clause})" for clause in clauses if clause) or "entire chart"


def _rows_preview(data: VizData, cap: int = _PROMPT_ROW_CAP) -> dict:
    return {
        "columns": [{"name": c.name, "field": c.fld, "kind": c.kind} for c in data.columns],
        "rows": data.rows[:cap],
        "truncated_for_prompt": len(data.rows) > cap,
    }


def _primary_driver_measure(spec: VizSpec) -> ChannelDef:
    """Driver shares need an additive measure — fall back to count otherwise."""
    for _, ch in spec.encoding.measures():
        if ch.aggregate in ("sum", "count"):
            return ChannelDef(field=ch.field, type="quantitative", aggregate=ch.aggregate)
    return ChannelDef(field=None, type="quantitative", aggregate="count")


async def _shares_by(
    state: Any,
    spec: VizSpec,
    dim: str,
    measure: ChannelDef,
    extra_filters: List[FilterTransform | FilterAnyTransform],
    sql_agent: Any,
    catalog: FieldCatalog,
) -> Dict[str, float]:
    probe = VizSpec(
        data=spec.data.model_copy(),
        transform=list(spec.transform) + list(extra_filters),
        mark=MarkDef(type="bar"),
        encoding=Encoding(
            x=ChannelDef.model_validate(
                {
                    "field": dim,
                    "type": "nominal",
                    "topK": {"n": _DRIVER_TOP_SEGMENTS, "others": True},
                }
            ),
            y=measure,
        ),
    )
    data = await run_spec(state, probe, sql_agent=sql_agent, catalog=catalog)
    pairs = [(str(r[0]), float(r[1])) for r in data.rows if r[1] is not None]
    total = sum(v for _, v in pairs)
    return {k: v / total for k, v in pairs} if total else {}


async def _driver_analysis(
    state: Any,
    spec: VizSpec,
    sel_filters: List[FilterTransform | FilterAnyTransform],
    catalog: FieldCatalog,
    sql_agent: Any,
) -> List[dict]:
    """How the selection's composition over OTHER fields differs from overall."""
    used = {ch.field for _, ch in spec.encoding.channel_items() if ch.field}
    for item in sel_filters:
        if isinstance(item, FilterTransform):
            used.add(item.field)
        else:
            used |= {f.field for clause in item.clauses for f in clause}
    candidates = [
        f.name
        for f in catalog.fields
        if f.type == "nominal"
        and not f.is_id
        and f.name not in used
        and f.cardinality is not None
        and 2 <= f.cardinality <= 30
    ][:MAX_DRIVER_DIMS]

    measure = _primary_driver_measure(spec)

    # Probes are independent read-only spec queries (run_sql checks out pooled
    # connections per call) — run them concurrently instead of serially.
    async def _probe(dim: str) -> Optional[List[Dict[str, float]]]:
        try:
            return await asyncio.gather(
                _shares_by(state, spec, dim, measure, sel_filters, sql_agent, catalog),
                _shares_by(state, spec, dim, measure, [], sql_agent, catalog),
            )
        except (SpecValidationError, ValueError) as exc:  # never fail the ask
            logger.info("[viz.inquiry] driver probe on %r skipped: %s", dim, exc)
            return None

    probes = await asyncio.gather(*(_probe(dim) for dim in candidates))

    findings: List[dict] = []
    for dim, probe in zip(candidates, probes):
        if probe is None:
            continue
        sel_shares, all_shares = probe
        for segment, sel_share in sel_shares.items():
            all_share = all_shares.get(segment)
            if all_share is None or sel_share < 0.05:
                continue
            findings.append(
                {
                    "field": dim,
                    "segment": segment,
                    "selected_share": round(sel_share, 4),
                    "overall_share": round(all_share, 4),
                    "lift": round(sel_share / all_share, 2) if all_share else None,
                }
            )
    findings.sort(key=lambda f: abs((f["lift"] or 1) - 1), reverse=True)
    return findings[:8]


async def _persist_drill(
    state: Any,
    raw_spec: Any,
    title: str,
    user_id: str,
    sql_agent: Any,
    source: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Validate + save a proposed drill-down. Returns (result, error_text)."""
    try:
        spec = VizSpec.model_validate(raw_spec)
        spec.data.source = source
        normalized, data = await materialize_spec(state, spec, sql_agent=sql_agent)
    except SpecValidationError as exc:
        return None, "; ".join(str(e) for e in exc.errors)
    except Exception as exc:  # noqa: BLE001 — schema/exec problems become repair feedback
        return None, str(exc)
    if not data.rows:
        return None, "drill_spec matched no rows"
    if normalized.title is None:
        normalized.title = title
    db = SessionLocal()
    try:
        row = viz_store.create_visualization(
            db,
            session_id=state.session_id,
            app_user_id=user_id,
            spec=normalized.to_json_dict(),
            title=title,
            dataset_ref=normalized.data.source,
            author="agent",
        )
        db.commit()
        viz_id = row.id
    finally:
        db.close()
    return {"viz_id": viz_id, "title": title}, None


async def ask_selection(
    state: Any,
    spec_dict: Dict[str, Any],
    selection_dict: Dict[str, Any],
    mode: str,
    question: Optional[str],
    *,
    graph_agent: Any,
    sql_agent: Any = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the full inquiry. Raises SpecValidationError for bad specs/selections."""
    from tools.viz_tools import SPEC_GUIDE, render_catalog_text  # noqa: PLC0415

    spec = VizSpec.model_validate(spec_dict)
    selection = Selection.model_validate(selection_dict)
    source_dc = resolve_data_context(state, spec.data.source)
    catalog = await get_field_catalog(state, sql_agent=sql_agent, data_context=source_dc)
    data = await run_spec(state, spec, sql_agent=sql_agent, catalog=catalog)

    sel_filters, errors = selection_to_filters(spec, selection)
    if errors:
        raise SpecValidationError(errors)

    selected = match_rows(data, selection)
    stats = selection_stats(data, selected)
    drivers = await _driver_analysis(state, spec, sel_filters, catalog, sql_agent)
    sel_text = describe_selection(spec, selection)

    instruction = _MODE_INSTRUCTIONS.get(mode, _MODE_INSTRUCTIONS["explain"])
    if mode == "custom" and question:
        instruction += f'\nUser question: "{question}"'

    evidence = {
        "chart_spec": spec.to_json_dict(),
        "selection": sel_text,
        "selection_filters": [f.model_dump() for f in sel_filters],
        "stats": stats,
        "driver_analysis": drivers,
        "chart_data_preview": _rows_preview(data),
    }
    messages = [
        # .replace, not .format — the prompt's JSON braces would break format().
        {"role": "system", "content": _SYSTEM_PROMPT.replace("{spec_guide}", SPEC_GUIDE)},
        {
            "role": "user",
            "content": (
                f"{render_catalog_text(catalog)}\n\n"
                f"TASK: {instruction}\n\n"
                f"EVIDENCE:\n{json.dumps(evidence, default=str)}"
            ),
        },
    ]
    response = await graph_agent.generate_response(messages, temperature=0.2)
    parsed = _parse_json_obj(response) or {}

    drill: Optional[dict] = None
    raw_drill = parsed.get("drill_spec")
    if raw_drill and user_id:
        title = str(parsed.get("drill_title") or "Drill-down")
        drill, err = await _persist_drill(
            state,
            raw_drill,
            title,
            user_id,
            sql_agent,
            str(source_dc.file_id),
        )
        if drill is None and err:
            # One repair round: feed the validation errors back, ask for a fixed spec.
            messages += [
                {"role": "assistant", "content": response},
                {
                    "role": "user",
                    "content": (
                        f"drill_spec was rejected: {err}\n"
                        "Return the SAME JSON object with a corrected drill_spec "
                        "(or drill_spec: null if it cannot be fixed)."
                    ),
                },
            ]
            retry = _parse_json_obj(await graph_agent.generate_response(messages, temperature=0.2))
            if retry and retry.get("drill_spec"):
                parsed = {**parsed, **{k: v for k, v in retry.items() if v}}
                drill, err = await _persist_drill(
                    state,
                    retry["drill_spec"],
                    str(retry.get("drill_title") or title),
                    user_id,
                    sql_agent,
                    str(source_dc.file_id),
                )
            if drill is None:
                logger.info("[viz.inquiry] drill spec dropped after repair: %s", err)

    return {
        "headline": str(parsed.get("headline") or "").strip() or "Selection analyzed",
        "insight": str(parsed.get("insight") or "").strip()
        or "I could not derive a reliable insight for this selection.",
        "selection": sel_text,
        "stats": stats,
        "drivers": drivers,
        "drill": drill,
    }

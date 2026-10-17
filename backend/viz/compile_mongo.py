"""
MongoDB execution of a QueryPlan — full-collection aggregation pushdown.

Renders the SAME semantics as ``viz.compile_pandas`` and ``viz.compile_sql``
(parity-tested): null dimension keys dropped, period starts emitted as ISO
strings with Monday-start weeks, equal-width bins with the last edge clamped,
topK ranked by the first measure with an optional re-aggregated "Other" bucket.

Where the SQL engine emits one statement with CTEs, this emits up to three
pipelines, because an aggregation cannot join a whole-collection statistic back
onto each document the way ``CROSS JOIN stats`` does:

    1. ``build_stats_pipeline``  — min/max per binned dimension  (one document)
    2. ``build_topk_pipeline``   — the top-n key values of one dimension
    3. ``compile_spec_mongo``    — the chart query, with 1 and 2 inlined

Each is a read-only aggregation that runs inside the customer's own cluster;
only the results cross the wire. Passes 1 and 2 exist only for the specs that
need them, so the ordinary chart is a single round trip.

Two differences from SQL are load-bearing rather than cosmetic:

- **Nulls sort low, not high.** SQL asks for ``NULLS LAST`` explicitly and
  pandas passes ``na_position="last"``; MongoDB has no such modifier, so every
  ordered channel gets a companion ``__null_<channel>`` rank that sorts first.
- **``$sum`` of nothing is 0, not null.** SQL ``SUM`` and pandas ``min_count=1``
  both yield null for an all-null group. A non-null counter per sum measure
  restores that, rather than letting an empty group read as a real zero.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .catalog import FieldCatalog
from .mongo_expr import (
    as_date,
    dim_key,
    dim_output,
    filters_expr,
    is_null,
    iso_string,
    not_null,
    num_expr,
    path,
)
from .plan import PlannedDim, PlannedMeasure, QueryPlan

OTHERS_LABEL = "Other (remaining)"


class UnsupportedVizFeature(ValueError):
    """A spec uses a feature the MongoDB engine does not implement."""


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def _measure_input(m: PlannedMeasure, catalog: FieldCatalog) -> Any:
    if m.aggregate == "distinct":
        return {"$ifNull": [path(str(m.fld)), None]}
    return num_expr(str(m.fld), catalog)


def _accumulators(plan: QueryPlan) -> Dict[str, Any]:
    """Group accumulators, keyed by internal field name."""
    acc: Dict[str, Any] = {}
    for m in plan.measures:
        src = f"$__m_{m.channel}"
        if m.aggregate == "count":
            acc[m.channel] = {"$sum": 1}
        elif m.aggregate == "distinct":
            acc[f"__set_{m.channel}"] = {"$addToSet": src}
        elif m.aggregate == "sum":
            acc[f"__sum_{m.channel}"] = {"$sum": src}
            # SUM over an all-null group is null, not zero, in both other engines.
            acc[f"__n_{m.channel}"] = {"$sum": {"$cond": [not_null(src), 1, 0]}}
        else:
            fn = {"mean": "avg", "min": "min", "max": "max"}[m.aggregate]
            acc[m.channel] = {f"${fn}": src}
    return acc


def _measure_output(m: PlannedMeasure) -> Any:
    if m.aggregate == "distinct":
        # COUNT(DISTINCT x) ignores nulls; $addToSet does not.
        return {
            "$size": {
                "$filter": {
                    "input": f"$__set_{m.channel}",
                    "cond": not_null("$$this"),
                }
            }
        }
    if m.aggregate == "sum":
        return {"$cond": [{"$gt": [f"$__n_{m.channel}", 0]}, f"$__sum_{m.channel}", None]}
    return f"${m.channel}"


# ---------------------------------------------------------------------------
# Preparatory pipelines
# ---------------------------------------------------------------------------


def _match_stage(plan: QueryPlan, catalog: FieldCatalog) -> List[Dict[str, Any]]:
    expr = filters_expr(plan.filters, catalog)
    return [{"$match": {"$expr": expr}}] if expr is not None else []


def _bin_value_stage(plan: QueryPlan, catalog: FieldCatalog) -> List[Dict[str, Any]]:
    """Project each binned dimension's numeric value once, as ``__v_<channel>``."""
    fields = {f"__v_{d.channel}": num_expr(d.fld, catalog) for d in plan.dims if d.kind == "bin"}
    return [{"$addFields": fields}] if fields else []


def build_stats_pipeline(plan: QueryPlan, catalog: FieldCatalog) -> List[Dict[str, Any]]:
    """Min/max per binned dimension, over the filtered collection. One document."""
    binned = [d for d in plan.dims if d.kind == "bin"]
    if not binned:
        return []
    group: Dict[str, Any] = {"_id": None}
    for d in binned:
        group[f"mn_{d.channel}"] = {"$min": f"$__v_{d.channel}"}
        group[f"mx_{d.channel}"] = {"$max": f"$__v_{d.channel}"}
    return _match_stage(plan, catalog) + _bin_value_stage(plan, catalog) + [{"$group": group}]


def stats_from_record(
    plan: QueryPlan, record: Optional[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Reshape the stats document into ``{channel: {"mn", "mx"}}``."""
    out: Dict[str, Dict[str, Any]] = {}
    for d in plan.dims:
        if d.kind != "bin":
            continue
        row = record or {}
        out[d.channel] = {"mn": row.get(f"mn_{d.channel}"), "mx": row.get(f"mx_{d.channel}")}
    return out


def _rank_accumulator(plan: QueryPlan) -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
    """The topK ranking measure as ``(field, accumulator, projection)``."""
    first = plan.measures[0]
    if first.aggregate == "count":
        return "__rank", {"$sum": 1}, None
    src = f"$__m_{first.channel}"
    if first.aggregate == "distinct":
        return (
            "__set",
            {"$addToSet": src},
            {"$size": {"$filter": {"input": "$__set", "cond": not_null("$$this")}}},
        )
    fn = {"mean": "avg", "sum": "sum", "min": "min", "max": "max"}[first.aggregate]
    return "__rank", {f"${fn}": src}, None


def build_topk_pipeline(
    plan: QueryPlan,
    catalog: FieldCatalog,
    dim: PlannedDim,
    stats: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The top-n values of one dimension, ranked by the plan's first measure.

    Each topK dimension is ranked independently against the same filtered input
    — ranking them in sequence would let one dimension's cut change another's.
    """
    n, _ = dim.top_k or (20, False)
    first = plan.measures[0]
    key = dim_key(dim, catalog, stats)
    field, accumulator, projection = _rank_accumulator(plan)

    stages = _match_stage(plan, catalog) + _bin_value_stage(plan, catalog)
    add: Dict[str, Any] = {"__k": key}
    if first.aggregate != "count":
        add[f"__m_{first.channel}"] = _measure_input(first, catalog)
    stages.append({"$addFields": add})
    stages.append({"$match": {"$expr": not_null("$__k")}})
    stages.append({"$group": {"_id": "$__k", field: accumulator}})
    if projection is not None:
        stages.append({"$addFields": {"__rank": projection}})
    # Measure DESC nulls last, key ASC — the same deterministic tie-break the
    # other two engines use, so an ambiguous cut resolves identically.
    stages.append({"$addFields": {"__null": {"$cond": [is_null("$__rank"), 1, 0]}}})
    stages.append({"$sort": {"__null": 1, "__rank": -1, "_id": 1}})
    stages.append({"$limit": int(n)})
    return stages


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def compile_spec_mongo(
    plan: QueryPlan,
    catalog: FieldCatalog,
    *,
    stats: Optional[Dict[str, Dict[str, Any]]] = None,
    top_values: Optional[Dict[str, List[Any]]] = None,
) -> List[Dict[str, Any]]:
    """The chart pipeline. ``stats`` and ``top_values`` come from the pipelines
    above; a spec with neither bins nor topK needs neither."""
    if plan.calcs:
        raise UnsupportedVizFeature(
            "Calculated fields are not yet supported on MongoDB sources. "
            "Remove the calculation, or chart this collection from a file extract."
        )
    stats = stats or {}
    tops = top_values or {}

    stages = _match_stage(plan, catalog) + _bin_value_stage(plan, catalog)
    keys: Dict[str, Any] = {}
    for d in plan.dims:
        keys[f"__k_{d.channel}"] = dim_key(d, catalog, stats)
    for m in plan.measures:
        if m.aggregate != "count":
            keys[f"__m_{m.channel}"] = _measure_input(m, catalog)
    for channel, fld, ftype in plan.extras:
        keys[f"__e_{channel}"] = as_date(path(fld)) if ftype == "temporal" else path(fld)
    if keys:
        stages.append({"$addFields": keys})

    stages.extend(_null_key_guard(plan))
    stages.extend(_topk_stages(plan, tops))

    if plan.aggregated:
        stages.extend(_aggregate_stages(plan, stats))
    else:
        stages.extend(_raw_stages(plan, stats))

    stages.extend(_order_stages(plan))
    stages.append({"$limit": int(plan.row_cap) + 1})
    return stages


def _null_key_guard(plan: QueryPlan) -> List[Dict[str, Any]]:
    """Aggregated output drops any null key; raw output only requires x and y."""
    if plan.aggregated:
        required = [d.channel for d in plan.dims]
    else:
        required = [d.channel for d in plan.dims if d.channel in ("x", "y")]
    if not required:
        return []
    terms = [not_null(f"$__k_{channel}") for channel in required]
    return [{"$match": {"$expr": terms[0] if len(terms) == 1 else {"$and": terms}}}]


def _topk_stages(plan: QueryPlan, tops: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """Restrict to the ranked values, or fold the remainder into "Other"."""
    stages: List[Dict[str, Any]] = []
    for d in plan.dims:
        if d.top_k is None:
            continue
        key = f"$__k_{d.channel}"
        values = tops.get(d.channel, [])
        member = {"$in": [key, {"$literal": values}]}
        if d.top_k[1]:
            stages.append(
                {"$addFields": {f"__k_{d.channel}": {"$cond": [member, key, OTHERS_LABEL]}}}
            )
        else:
            stages.append({"$match": {"$expr": member}})
    return stages


def _aggregate_stages(plan: QueryPlan, stats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Channel names are the _id sub-keys: a grouping key may not contain a dot,
    # and a flattened Mongo column routinely does.
    group_id: Any = {d.channel: f"$__k_{d.channel}" for d in plan.dims} if plan.dims else None
    group: Dict[str, Any] = {"_id": group_id, **_accumulators(plan)}

    projection: Dict[str, Any] = {"_id": 0}
    for d in plan.dims:
        projection[d.channel] = dim_output(d, f"$_id.{d.channel}")
    for m in plan.measures:
        projection[m.channel] = _measure_output(m)
    projection.update(_bin_width_fields(plan, stats))
    return [{"$group": group}, {"$project": projection}]


def _raw_stages(plan: QueryPlan, stats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    projection: Dict[str, Any] = {"_id": 0}
    for d in plan.dims:
        projection[d.channel] = dim_output(d, f"$__k_{d.channel}")
    for channel, _fld, ftype in plan.extras:
        source = f"$__e_{channel}"
        projection[channel] = iso_string(source) if ftype == "temporal" else source
    projection.update(_bin_width_fields(plan, stats))
    return [{"$project": projection}]


def _bin_width_fields(plan: QueryPlan, stats: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Bin width as a literal — it is a constant of the stats pass, so it needs
    no per-document field. ``extract_hidden_meta`` pops these from the records."""
    out: Dict[str, Any] = {}
    for d in plan.dims:
        if d.kind != "bin":
            continue
        mn, mx = stats.get(d.channel, {}).get("mn"), stats.get(d.channel, {}).get("mx")
        if mn is None or mx is None:
            continue
        width = 1.0 if mx == mn else (float(mx) - float(mn)) / int(d.maxbins or 10)
        out[f"__bin_w_{d.channel}"] = {"$literal": width}
    return out


def _order_stages(plan: QueryPlan) -> List[Dict[str, Any]]:
    """Sort with nulls last, which MongoDB has no modifier for."""
    order = [(channel, direction) for channel, direction in plan.order]
    if not order:
        return []
    ranks = {f"__null_{c}": {"$cond": [is_null(f"${c}"), 1, 0]} for c, _ in order}
    sort: Dict[str, Any] = {}
    for channel, direction in order:
        sort[f"__null_{channel}"] = 1
        sort[channel] = 1 if direction == "asc" else -1
    return [
        {"$addFields": ranks},
        {"$sort": sort},
        {"$project": {name: 0 for name in ranks}},
    ]

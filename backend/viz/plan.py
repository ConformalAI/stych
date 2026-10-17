"""
Query plan — the single place where VizSpec semantics become decisions.

Both compilers (pandas and SQL) consume the SAME plan, so grouping keys, topK
ranking, sort resolution, and row caps can never diverge between engines. Any
new semantic (a mark type, a timeUnit) starts here.

Output contract (``VizData``) is the tidy-table seam shared with the frontend
``compileSpec.ts``: one column per channel, named after the channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from .expr import ResolvedCalc
from .spec import FilterAnyTransform, FilterTransform, SortByEncoding, VizSpec

AGGREGATED_ROW_CAP = 2000
RAW_ROW_CAP = 10000

DimKind = Literal["raw", "time", "bin"]


@dataclass
class PlannedDim:
    channel: str  # x | y | color | size
    fld: str
    kind: DimKind
    field_type: str  # quantitative | nominal | ordinal | temporal
    time_unit: Optional[str] = None
    maxbins: Optional[int] = None
    top_k: Optional[Tuple[int, bool]] = None  # (n, keep_others)


@dataclass
class PlannedMeasure:
    channel: str
    aggregate: str  # sum | mean | min | max | count | distinct
    fld: Optional[str] = None  # None only for count


@dataclass
class QueryPlan:
    aggregated: bool
    dims: List[PlannedDim] = field(default_factory=list)
    measures: List[PlannedMeasure] = field(default_factory=list)
    filters: List[FilterTransform | FilterAnyTransform] = field(default_factory=list)
    # Calculated columns (viz.expr), projected before filters in BOTH engines.
    calcs: List[ResolvedCalc] = field(default_factory=list)
    # Raw-mode extra projections (tooltip fields), (channel_key, field, type).
    extras: List[Tuple[str, str, str]] = field(default_factory=list)
    # Resolved total order: list of (channel, "asc"|"desc"), most-significant first.
    order: List[Tuple[str, str]] = field(default_factory=list)
    row_cap: int = AGGREGATED_ROW_CAP

    def channels(self) -> List[str]:
        """All output channel keys in column order (dims, then measures, then extras)."""
        out = [d.channel for d in self.dims] + [m.channel for m in self.measures]
        out.extend(k for k, _, _ in self.extras)
        return out


class VizColumn(BaseModel):
    name: str  # channel key — the stable contract with the frontend
    fld: Optional[str] = Field(default=None, alias="field")
    kind: str  # dimension | measure
    type: str  # quantitative | nominal | ordinal | temporal

    model_config = {"populate_by_name": True}


class VizData(BaseModel):
    columns: List[VizColumn]
    rows: List[List[Any]]
    meta: Dict[str, Any] = Field(default_factory=dict)


def plan_columns(plan: QueryPlan) -> List[VizColumn]:
    """Output column descriptors in the tidy-table contract order."""
    cols = [
        VizColumn(name=d.channel, fld=d.fld, kind="dimension", type=d.field_type) for d in plan.dims
    ]
    cols += [
        VizColumn(name=m.channel, fld=m.fld, kind="measure", type="quantitative")
        for m in plan.measures
    ]
    cols += [VizColumn(name=k, fld=f, kind="dimension", type=t) for k, f, t in plan.extras]
    return cols


def build_plan(spec: VizSpec, calcs: Optional[List[ResolvedCalc]] = None) -> QueryPlan:
    """``calcs`` are the spec's resolved calculations (viz.expr) when it has any."""
    enc = spec.encoding
    dims: List[PlannedDim] = []
    measures: List[PlannedMeasure] = []

    for name, ch in enc.channel_items():
        if ch.is_measure:
            measures.append(PlannedMeasure(channel=name, aggregate=str(ch.aggregate), fld=ch.field))
            continue
        assert ch.field is not None  # validate_spec guarantees this
        if ch.bin is not None:
            kind: DimKind = "bin"
        elif ch.time_unit is not None:
            kind = "time"
        else:
            kind = "raw"
        dims.append(
            PlannedDim(
                channel=name,
                fld=ch.field,
                kind=kind,
                field_type=ch.type,
                time_unit=ch.time_unit,
                maxbins=ch.bin.maxbins if ch.bin else None,
                top_k=(ch.top_k.n, ch.top_k.others) if ch.top_k else None,
            )
        )

    aggregated = bool(measures)
    plan = QueryPlan(
        aggregated=aggregated,
        dims=dims,
        measures=measures,
        filters=spec.filters(),
        calcs=list(calcs or []),
        row_cap=AGGREGATED_ROW_CAP if aggregated else RAW_ROW_CAP,
    )

    if not aggregated:
        seen = {d.fld for d in dims}
        for i, ch in enumerate(enc.tooltip):
            if ch.field and ch.field not in seen:
                plan.extras.append((f"tooltip_{i}", ch.field, ch.type))
                seen.add(ch.field)

    plan.order = _resolve_order(spec, plan)
    return plan


def _resolve_order(spec: VizSpec, plan: QueryPlan) -> List[Tuple[str, str]]:
    """
    Deterministic total order shared by both engines.

    1. An explicit channel ``sort`` wins (channel priority x, y, color, size).
       A bare ``"asc"``/``"desc"`` orders by that channel's own values; a
       sort-by-encoding (``{"encoding":"y","order":"desc"}``) orders by the
       referenced channel instead — e.g. bars sorted by their measure.
    2. Otherwise a temporal or binned x orders ascending (time series / histogram).
    3. Otherwise aggregated output orders by the first measure descending.
    4. Remaining dimension channels are ascending tie-breakers, so equal
       primary keys can never produce engine-dependent row order.
    Raw (non-aggregated) output has no implied order beyond an explicit sort.
    """
    enc = spec.encoding
    primary: List[Tuple[str, str]] = []

    for name in ("x", "y", "color", "size"):
        ch = getattr(enc, name)
        if ch is None or ch.sort is None:
            continue
        if isinstance(ch.sort, SortByEncoding):
            primary.append((ch.sort.encoding, ch.sort.order))
        else:
            primary.append((name, ch.sort))
        break

    if not primary:
        x_dim = next((d for d in plan.dims if d.channel == "x"), None)
        if x_dim is not None and (x_dim.kind in ("time", "bin") or x_dim.field_type == "temporal"):
            primary.append(("x", "asc"))
        elif plan.aggregated:
            primary.append((plan.measures[0].channel, "desc"))

    if not plan.aggregated:
        return primary

    ordered_channels = {c for c, _ in primary}
    tiebreak = [(d.channel, "asc") for d in plan.dims if d.channel not in ordered_channels]
    return primary + tiebreak

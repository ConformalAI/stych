"""
Chart selections — the wire format for "ask this chart".

A Selection is what the user has highlighted on a rendered chart, expressed in
the spec's own vocabulary (channels + values/ranges) rather than pixels, so it
is exact, replayable, and engine-agnostic. Two consumers:

- ``match_rows``: which tidy-table rows (viz.plan contract) fall inside the
  selection — powers deterministic selected-vs-rest stats.
- ``selection_to_filters``: the same selection as FilterTransforms over the raw
  fields — powers drill-down / driver-analysis queries through ``run_spec`` on
  either engine.

Selections use OR-of-AND clauses, preserving the tuples behind selected chart
marks. This prevents a sparse heatmap or stacked-bar selection from silently
expanding into the cross-product of its axis values.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .plan import VizData
from .spec import ChannelDef, FilterAnyTransform, FilterTransform, SpecError, VizSpec

_TIME_UNIT_MONTHS = {"year": 12, "quarter": 3, "month": 1}


class SelectionRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    min: Any
    max: Any
    # True when max is a bucket START (temporal buckets): match < max, not <= max.
    exclusive_max: bool = Field(default=False, alias="exclusiveMax")


class ChannelSelection(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    channel: Literal["x", "y", "color", "size"]
    values: Optional[List[Any]] = None
    range: Optional[SelectionRange] = None


class SelectionClause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ChannelSelection] = Field(min_length=1)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``items`` remains supported for older clients. New clients use clauses:
    # OR between clauses, AND within each clause.
    items: Optional[List[ChannelSelection]] = None
    clauses: Optional[List[SelectionClause]] = Field(default=None, max_length=250)

    @model_validator(mode="after")
    def require_one_shape(self):
        if bool(self.items) == bool(self.clauses):
            raise ValueError("Selection needs exactly one of 'items' or 'clauses'.")
        return self


def _clauses(selection: Selection) -> List[List[ChannelSelection]]:
    if selection.clauses is not None:
        return [clause.items for clause in selection.clauses]
    return [selection.items or []]


# ---------------------------------------------------------------------------
# Tidy-row matching (deterministic stats over the materialized table)
# ---------------------------------------------------------------------------


def match_rows(data: VizData, selection: Selection) -> List[int]:
    """Indices of tidy rows inside the selection's OR-of-AND clauses."""
    col_idx = {c.name: i for i, c in enumerate(data.columns)}
    clauses = [
        [(col_idx[item.channel], item) for item in clause if item.channel in col_idx]
        for clause in _clauses(selection)
    ]

    out: List[int] = []
    for ri, row in enumerate(data.rows):
        if any(all(_cell_matches(row[ci], item) for ci, item in checks) for checks in clauses):
            out.append(ri)
    return out


def _cell_matches(value: Any, item: ChannelSelection) -> bool:
    if value is None:
        return False
    if item.values is not None:
        return str(value) in {str(v) for v in item.values}
    if item.range is not None:
        lo, hi, excl = item.range.min, item.range.max, item.range.exclusive_max
        if isinstance(value, (int, float)):
            try:
                v, lo_f, hi_f = float(value), float(lo), float(hi)
            except (TypeError, ValueError):
                return False
            return lo_f <= v and (v < hi_f if excl else v <= hi_f)
        # ISO period-start strings compare lexicographically == chronologically.
        v_s, lo_s, hi_s = str(value), str(lo), str(hi)
        return lo_s <= v_s and (v_s < hi_s if excl else v_s <= hi_s)
    return False


# ---------------------------------------------------------------------------
# Selection → raw-field filters (for drill-downs / driver queries)
# ---------------------------------------------------------------------------


def selection_to_filters(
    spec: VizSpec, selection: Selection
) -> Tuple[List[FilterTransform | FilterAnyTransform], List[SpecError]]:
    clause_filters: List[List[FilterTransform]] = []
    errors: List[SpecError] = []
    for clause_i, clause in enumerate(_clauses(selection)):
        filters: List[FilterTransform] = []
        for item in clause:
            ch: Optional[ChannelDef] = getattr(spec.encoding, item.channel, None)
            path = f"selection.clauses[{clause_i}].{item.channel}"
            if ch is None or ch.field is None:
                errors.append(
                    SpecError(
                        path=path,
                        code="selection_channel_empty",
                        message=f"Channel '{item.channel}' has no field to select on.",
                    )
                )
                continue
            if ch.is_measure:
                errors.append(
                    SpecError(
                        path=path,
                        code="selection_on_measure",
                        message="Selections on aggregated measures cannot become row filters.",
                    )
                )
                continue
            if item.range is not None:
                r = item.range
                filters.extend(
                    [
                        FilterTransform(field=ch.field, predicate="gte", value=r.min),
                        FilterTransform(
                            field=ch.field,
                            predicate="lt" if r.exclusive_max else "lte",
                            value=r.max,
                        ),
                    ]
                )
            elif item.values:
                if any(str(value) == "Other (remaining)" for value in item.values):
                    errors.append(
                        SpecError(
                            path=path,
                            code="selection_topk_other",
                            message="The 'Other' bucket combines multiple values and cannot be selected exactly.",
                        )
                    )
                elif ch.time_unit is not None:
                    starts = sorted(set(str(v) for v in item.values))
                    unit = str(ch.time_unit)
                    if any(
                        _bucket_end(left, unit) != right for left, right in zip(starts, starts[1:])
                    ):
                        errors.append(
                            SpecError(
                                path=path,
                                code="selection_not_exact",
                                message="Separated time periods cannot be represented exactly yet.",
                            )
                        )
                    else:
                        lo, hi = _bucket_envelope(starts, unit)
                        filters.extend(
                            [
                                FilterTransform(field=ch.field, predicate="gte", value=lo),
                                FilterTransform(field=ch.field, predicate="lt", value=hi),
                            ]
                        )
                elif ch.bin is not None:
                    errors.append(
                        SpecError(
                            path=path,
                            code="selection_bin_needs_range",
                            message="Binned channels need a range selection, not values.",
                        )
                    )
                else:
                    filters.append(
                        FilterTransform(field=ch.field, predicate="in", value=item.values)
                    )
            else:
                errors.append(
                    SpecError(
                        path=path,
                        code="selection_empty",
                        message="Selection item needs values or a range.",
                    )
                )
        clause_filters.append(filters)
    if errors:
        return [], errors
    if len(clause_filters) == 1:
        return clause_filters[0], []
    return [FilterAnyTransform(clauses=clause_filters)], []


def _bucket_envelope(starts: List[str], unit: str) -> Tuple[str, str]:
    """[min bucket start, end of max bucket) for timeUnit values."""
    lo, hi_start = min(starts), max(starts)
    return lo, _bucket_end(hi_start, unit)


def _bucket_end(start_iso: str, unit: str) -> str:
    date_part = start_iso.split("T")[0]
    y, m, d = (int(p) for p in date_part.split("-"))
    hour = int(start_iso.split("T")[1][:2]) if "T" in start_iso else 0
    if unit in _TIME_UNIT_MONTHS:
        months = y * 12 + (m - 1) + _TIME_UNIT_MONTHS[unit]
        return f"{months // 12:04d}-{months % 12 + 1:02d}-01T00:00:00"
    if unit == "hour":
        # day rollover only — hour buckets beyond 22:00 fall to next day 00:00
        if hour < 23:
            return f"{y:04d}-{m:02d}-{d:02d}T{hour + 1:02d}:00:00"
        unit, hour = "day", 0  # fall through to day arithmetic
    days = 7 if unit == "week" else 1
    import datetime as _dt  # noqa: PLC0415

    nxt = _dt.date(y, m, d) + _dt.timedelta(days=days)
    return f"{nxt.year:04d}-{nxt.month:02d}-{nxt.day:02d}T00:00:00"


# ---------------------------------------------------------------------------
# Deterministic selected-vs-rest stats
# ---------------------------------------------------------------------------


def selection_stats(data: VizData, selected: List[int]) -> dict:
    """Per-measure totals/averages for the selected tidy rows vs the whole view."""
    sel = set(selected)
    measures = [(i, c) for i, c in enumerate(data.columns) if c.kind == "measure"]
    out: dict = {
        "selected_points": len(sel),
        "total_points": len(data.rows),
        "measures": [],
    }
    for ci, col in measures:
        sel_vals = [r[ci] for i, r in enumerate(data.rows) if i in sel and r[ci] is not None]
        all_vals = [r[ci] for r in data.rows if r[ci] is not None]
        m: dict = {"channel": col.name, "field": col.fld}
        if sel_vals:
            m["selected_sum"] = round(float(sum(sel_vals)), 6)
            m["selected_avg"] = round(float(sum(sel_vals) / len(sel_vals)), 6)
        if all_vals:
            total = float(sum(all_vals))
            m["total_sum"] = round(total, 6)
            m["overall_avg"] = round(total / len(all_vals), 6)
            if sel_vals and total:
                m["selected_share"] = round(float(sum(sel_vals)) / total, 4)
        out["measures"].append(m)
    return out

"""
Pandas execution of a QueryPlan (file sessions).

Semantics are defined by ``viz.plan`` — this module only renders them onto a
DataFrame. Must stay row-for-row equivalent with ``viz.compile_sql`` (parity
tests enforce it): null dimension keys are dropped, timeUnit floors to the
period start (ISO string, Monday-start weeks like Postgres ``date_trunc``),
bins are equal-width with the last edge clamped, topK ranks by the first
measure and can lump the remainder into "Other" (re-aggregated from raw rows,
so non-additive aggregates like mean stay correct).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .expr import materialize_pandas
from .plan import PlannedDim, PlannedMeasure, QueryPlan, VizData, plan_columns
from .shaping import _json_value
from .spec import FilterAnyTransform, FilterTransform

OTHERS_LABEL = "Other (remaining)"


def execute_pandas(df: pd.DataFrame, plan: QueryPlan) -> VizData:
    work = _apply_calcs(df, plan)
    work = _apply_filters(work, plan)

    if plan.aggregated:
        rows, meta = _run_aggregated(work, plan)
    else:
        rows, meta = _run_raw(work, plan)

    truncated = len(rows) > plan.row_cap
    if truncated:
        rows = rows[: plan.row_cap]
    meta.update({"row_count": len(rows), "truncated": truncated, "engine": "pandas"})
    return VizData(columns=plan_columns(plan), rows=rows, meta=meta)


# ---------------------------------------------------------------------------
# Calculated columns (projected before filters — SQL engine does the same)
# ---------------------------------------------------------------------------


def _apply_calcs(df: pd.DataFrame, plan: QueryPlan) -> pd.DataFrame:
    if not plan.calcs:
        return df
    return df.assign(**{c.name: materialize_pandas(c.ast, df) for c in plan.calcs})


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _apply_filters(df: pd.DataFrame, plan: QueryPlan) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for t in plan.filters:
        if isinstance(t, FilterAnyTransform):
            clause_mask = pd.Series(False, index=df.index)
            for clause in t.clauses:
                current = pd.Series(True, index=df.index)
                for item in clause:
                    current &= _filter_mask(df, item)
                clause_mask |= current
            mask &= clause_mask
        else:
            mask &= _filter_mask(df, t)
    return df[mask]


def _filter_mask(df: pd.DataFrame, t: FilterTransform) -> pd.Series:
    s = df[t.field]
    p, v = t.predicate, t.value
    if p in ("isnull", "notnull"):
        return s.isnull() if p == "isnull" else s.notnull()
    if p == "contains":
        return s.astype("string").str.contains(str(v), case=False, na=False, regex=False)
    if p in ("in", "notin"):
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            # Numeric membership compares numerically — float text formatting
            # differs between engines (pandas "5.0" vs Postgres float8 "5").
            nums = []
            for x in v or []:
                try:
                    nums.append(float(x))
                except (TypeError, ValueError):
                    continue
            m = s.isin(nums)
        else:
            vals = [str(x) for x in (v or [])]
            m = s.astype("string").isin(vals)
        return (~m & s.notnull()) if p == "notin" else m

    left, right = _comparable(s, v if p != "between" else v[0])
    if p == "eq":
        m = left == right
    elif p == "neq":
        m = (left != right) & s.notnull()
    elif p == "gt":
        m = left > right
    elif p == "gte":
        m = left >= right
    elif p == "lt":
        m = left < right
    elif p == "lte":
        m = left <= right
    else:  # between
        lo, hi = _comparable(s, v[0])[1], _comparable(s, v[1])[1]
        left = _comparable(s, v[0])[0]
        m = (left >= lo) & (left <= hi)
    return m.fillna(False)


def _comparable(s: pd.Series, v: Any) -> tuple:
    """Coerce series+literal into the same comparable domain (num, date, or str)."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s, pd.to_datetime(v)
    if pd.api.types.is_numeric_dtype(s):
        return s, float(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return pd.to_numeric(s, errors="coerce"), float(v)
    # Temporal-looking string literal against a text column (catalog said temporal).
    parsed_v = pd.to_datetime(v, errors="coerce") if isinstance(v, str) else pd.NaT
    if parsed_v is not pd.NaT and not pd.isna(parsed_v):
        parsed_s = pd.to_datetime(s, errors="coerce")
        if parsed_s.notna().any():
            return parsed_s, parsed_v
    return s.astype("string"), str(v)


# ---------------------------------------------------------------------------
# Dimension key derivation
# ---------------------------------------------------------------------------


def _time_floor(s: pd.Series, unit: str) -> pd.Series:
    dt = pd.to_datetime(s, errors="coerce")
    if unit == "year":
        return dt.dt.to_period("Y").dt.start_time
    if unit == "quarter":
        return dt.dt.to_period("Q").dt.start_time
    if unit == "month":
        return dt.dt.to_period("M").dt.start_time
    if unit == "week":  # Monday start — matches Postgres date_trunc('week')
        return dt.dt.normalize() - pd.to_timedelta(dt.dt.weekday, unit="D")
    if unit == "day":
        return dt.dt.normalize()
    return dt.dt.floor("h")  # hour


def _derive_key(work: pd.DataFrame, d: PlannedDim, meta: Dict[str, Any]) -> pd.Series:
    s = work[d.fld]
    if d.kind == "time":
        return _time_floor(s, str(d.time_unit))
    if d.kind == "bin":
        v = pd.to_numeric(s, errors="coerce")
        mn, mx = float(v.min()), float(v.max())
        maxbins = int(d.maxbins or 10)
        finite = np.isfinite(mn) and np.isfinite(mx)
        if not finite:
            # Keep the metadata strictly JSON-safe. There are no output bins
            # when every value is null, so no width is needed by the renderer.
            meta.setdefault("bins", {})[d.channel] = {
                "min": None,
                "max": None,
                "width": None,
            }
            return pd.Series(np.nan, index=v.index)
        if mx == mn:
            width = 1.0
            starts = pd.Series(mn, index=v.index).where(v.notna())
        else:
            width = (mx - mn) / maxbins
            idx = np.clip(np.floor((v - mn) / width), 0, maxbins - 1)
            starts = mn + idx * width
        meta.setdefault("bins", {})[d.channel] = {"min": mn, "max": mx, "width": width}
        return pd.Series(starts, index=v.index).round(10)
    if d.field_type == "temporal":
        return pd.to_datetime(s, errors="coerce")
    if d.field_type == "quantitative":
        return pd.to_numeric(s, errors="coerce")
    return s.astype("string")


# ---------------------------------------------------------------------------
# Aggregated path
# ---------------------------------------------------------------------------


def _measure_series(work: pd.DataFrame, m: PlannedMeasure) -> Optional[pd.Series]:
    if m.aggregate == "count":
        return None
    s = work[str(m.fld)]
    if m.aggregate == "distinct":
        return s
    return pd.to_numeric(s, errors="coerce")


def _agg_one(grouped, m: PlannedMeasure, key: str):
    if m.aggregate == "count":
        return grouped.size()
    col = grouped[key]
    if m.aggregate == "distinct":
        return col.nunique()
    if m.aggregate == "sum":
        # min_count=1 → all-null group sums to NaN, matching SQL SUM's NULL.
        return col.sum(min_count=1)
    return getattr(col, {"mean": "mean", "min": "min", "max": "max"}[m.aggregate])()


def _run_aggregated(work: pd.DataFrame, plan: QueryPlan) -> tuple:
    meta: Dict[str, Any] = {}
    frame = pd.DataFrame(index=work.index)
    for d in plan.dims:
        frame[f"__k_{d.channel}"] = _derive_key(work, d, meta)
    for m in plan.measures:
        s = _measure_series(work, m)
        if s is not None:
            frame[f"__m_{m.channel}"] = s

    key_cols = [f"__k_{d.channel}" for d in plan.dims]
    if key_cols:
        frame = frame.dropna(subset=key_cols)

    # Rank every topK dimension independently against the same filtered input.
    # Mutating one dimension before ranking the next made pandas semantics
    # sequential while SQL ranked each dimension independently.
    ranked: Dict[str, set] = {}
    for d in plan.dims:
        if d.top_k is None:
            continue
        key = f"__k_{d.channel}"
        first = plan.measures[0]
        per_dim = _agg_one(frame.groupby(key, sort=False), first, f"__m_{first.channel}")
        # SQL uses measure DESC NULLS LAST, key ASC as a deterministic tie-break.
        ranking = per_dim.rename("__rank").reset_index()
        ranking = ranking.sort_values(
            by=["__rank", key],
            ascending=[False, True],
            kind="stable",
            na_position="last",
        )
        ranked[d.channel] = set(ranking.head(d.top_k[0])[key])

    for d in plan.dims:
        if d.top_k is None:
            continue
        _, keep_others = d.top_k
        key = f"__k_{d.channel}"
        top = ranked[d.channel]
        if keep_others:
            frame[key] = frame[key].where(frame[key].isin(top), OTHERS_LABEL)
        else:
            frame = frame[frame[key].isin(top)]

    if key_cols:
        grouped = frame.groupby(key_cols, sort=False, dropna=True)
        out = pd.DataFrame(
            {m.channel: _agg_one(grouped, m, f"__m_{m.channel}") for m in plan.measures}
        ).reset_index()
        out.columns = [c.replace("__k_", "") if isinstance(c, str) else c for c in out.columns]
    else:  # pure aggregate, single row
        row = {}
        for m in plan.measures:
            if m.aggregate == "count":
                row[m.channel] = len(frame)
            elif m.aggregate == "distinct":
                row[m.channel] = frame[f"__m_{m.channel}"].nunique()
            elif m.aggregate == "sum":
                row[m.channel] = frame[f"__m_{m.channel}"].sum(min_count=1)
            else:
                agg = {"mean": "mean", "min": "min", "max": "max"}[m.aggregate]
                row[m.channel] = getattr(frame[f"__m_{m.channel}"], agg)()
        out = pd.DataFrame([row])

    out = _sort(out, plan).head(plan.row_cap + 1)
    channels = [d.channel for d in plan.dims] + [m.channel for m in plan.measures]
    dim_types = {d.channel: d for d in plan.dims}
    rows = [[_json_value(row[c], dim_types.get(c)) for c in channels] for _, row in out.iterrows()]
    return rows, meta


# ---------------------------------------------------------------------------
# Raw path (unaggregated scatter/line)
# ---------------------------------------------------------------------------


def _run_raw(work: pd.DataFrame, plan: QueryPlan) -> tuple:
    meta: Dict[str, Any] = {}
    out = pd.DataFrame(index=work.index)
    required: List[str] = []
    for d in plan.dims:
        out[d.channel] = _derive_key(work, d, meta)
        if d.channel in ("x", "y"):
            required.append(d.channel)
    for k, f, t in plan.extras:
        col = work[f]
        out[k] = pd.to_datetime(col, errors="coerce") if t == "temporal" else col

    if required:
        out = out.dropna(subset=required)
    out = _sort(out, plan).head(plan.row_cap + 1)

    dim_types = {d.channel: d for d in plan.dims}
    channels = plan.channels()
    rows = [[_json_value(row[c], dim_types.get(c)) for c in channels] for _, row in out.iterrows()]
    return rows, meta


# ---------------------------------------------------------------------------
# Shared output helpers
# ---------------------------------------------------------------------------


def _sort(out: pd.DataFrame, plan: QueryPlan) -> pd.DataFrame:
    by = [(c, d) for c, d in plan.order if c in out.columns]
    if not by:
        return out
    return out.sort_values(
        by=[c for c, _ in by],
        ascending=[d == "asc" for _, d in by],
        kind="stable",
        na_position="last",
    )

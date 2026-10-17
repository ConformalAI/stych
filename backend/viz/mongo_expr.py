"""
Aggregation *expressions* for the MongoDB viz engine.

Split from ``viz.compile_mongo`` so pipeline assembly stays readable: this
module answers "what is this value?" and that one answers "how many passes?".

Every coercion maps both failure and absence to null, matching pandas'
``errors="coerce"``, and every ordered comparison is null-guarded — BSON sorts
null below every number, so an unguarded ``null < 5`` would be true here and
false in both other engines.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .catalog import PG_NUMERIC_TYPES, FieldCatalog
from .plan import PlannedDim
from .spec import FilterAnyTransform, FilterTransform

#: Matches ``SqlDialect.format_iso_timestamp`` and pandas' ``_ISO_FMT``.
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"
#: pandas rounds bin starts to 10 decimals before grouping; float division
#: leaves the same dust here, so equal starts must land on one key.
BIN_ROUND = 10

# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def path(fld: str) -> str:
    """A flattened column name is already a Mongo field path (``a.b`` → ``$a.b``)."""
    return f"${fld}"


def _convert(expr: Any, to: str) -> Dict[str, Any]:
    """Coerce, mapping both failure and absence to null — pandas ``errors="coerce"``."""
    return {"$convert": {"input": expr, "to": to, "onError": None, "onNull": None}}


def _as_num(expr: Any) -> Dict[str, Any]:
    return _convert(expr, "double")


def as_date(expr: Any) -> Dict[str, Any]:
    return _convert(expr, "date")


def _as_text(expr: Any) -> Dict[str, Any]:
    return _convert(expr, "string")


def is_null(expr: Any) -> Dict[str, Any]:
    """True when the value is null OR the field is absent."""
    return {"$eq": [{"$ifNull": [expr, None]}, None]}


def not_null(expr: Any) -> Dict[str, Any]:
    return {"$ne": [{"$ifNull": [expr, None]}, None]}


def _false(expr: Any) -> Dict[str, Any]:
    """``expr`` is false. Written this way because ``$not``'s argument arity is
    ambiguous across drivers; ``$eq``-to-false has one reading everywhere."""
    return {"$eq": [expr, False]}


def num_expr(fld: str, catalog: FieldCatalog) -> Any:
    """Numeric-valued expression; non-numeric storage gets a coercing cast."""
    info = catalog.get(fld)
    db_type = ((info.db_type or "") if info is not None else "").lower()
    if db_type in PG_NUMERIC_TYPES or any(
        token in db_type
        for token in ("int", "numeric", "decimal", "real", "double", "float", "money")
    ):
        return {"$ifNull": [path(fld), None]}
    return _as_num(path(fld))


def iso_string(expr: Any) -> Dict[str, Any]:
    """Format a date as the shared ISO string, so every engine emits one shape."""
    return {"$dateToString": {"date": expr, "format": ISO_FORMAT, "onNull": None}}


# ---------------------------------------------------------------------------
# Dimension keys
# ---------------------------------------------------------------------------


def _time_key(d: PlannedDim) -> Dict[str, Any]:
    unit = str(d.time_unit)
    trunc: Dict[str, Any] = {"date": as_date(path(d.fld)), "unit": unit}
    if unit == "week":
        # Postgres date_trunc('week') and the pandas engine both start Monday;
        # $dateTrunc would otherwise start Sunday.
        trunc["startOfWeek"] = "monday"
    return {"$dateTrunc": trunc}


def _bin_key(d: PlannedDim, stats: Dict[str, Any]) -> Any:
    """Equal-width bin start, last edge clamped, from precomputed min/max."""
    value = {"$ifNull": [f"$__v_{d.channel}", None]}
    mn, mx = stats.get("mn"), stats.get("mx")
    if mn is None or mx is None:
        return None  # every value was null; there are no bins to form
    maxbins = int(d.maxbins or 10)
    if mx == mn:
        return {"$cond": [is_null(value), None, float(mn)]}
    width = (float(mx) - float(mn)) / maxbins
    index = {
        "$min": [{"$floor": {"$divide": [{"$subtract": [value, float(mn)]}, width]}}, maxbins - 1]
    }
    start = {"$add": [float(mn), {"$multiply": [index, width]}]}
    return {"$cond": [is_null(value), None, {"$round": [start, BIN_ROUND]}]}


def dim_key(d: PlannedDim, catalog: FieldCatalog, stats: Dict[str, Dict[str, Any]]) -> Any:
    if d.kind == "time":
        return _time_key(d)
    if d.kind == "bin":
        return _bin_key(d, stats.get(d.channel, {}))
    if d.field_type == "temporal":
        return as_date(path(d.fld))
    if d.field_type == "quantitative":
        return num_expr(d.fld, catalog)
    return _as_text(path(d.fld))


def dim_output(d: PlannedDim, expr: Any) -> Any:
    """Format a key for the output row. Dates become ISO strings here, so the
    driver never hands the caller a datetime the other engines would not."""
    if d.kind == "time" or d.field_type == "temporal":
        return iso_string(expr)
    return expr


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _filter_expr(t: FilterTransform, catalog: FieldCatalog) -> Dict[str, Any]:
    raw = {"$ifNull": [path(t.field), None]}
    info = catalog.get(t.field)
    ftype = info.type if info is not None else "nominal"
    p, v = t.predicate, t.value

    if p == "isnull":
        return is_null(raw)
    if p == "notnull":
        return not_null(raw)
    if p == "contains":
        # Literal substring, case-insensitive — the other engines use
        # regex=False / ILIKE, so the pattern must not be read as a regex.
        return {
            "$and": [
                not_null(raw),
                {
                    "$regexMatch": {
                        "input": _as_text(raw),
                        "regex": re.escape(str(v)),
                        "options": "i",
                    }
                },
            ]
        }
    if p in ("in", "notin"):
        if ftype == "quantitative":
            # Numeric membership compares numerically — float text formatting
            # differs between engines (pandas "5.0" vs float8 "5").
            values: List[Any] = []
            for x in v or []:
                try:
                    values.append(float(x))
                except (TypeError, ValueError):
                    continue
            left: Any = num_expr(t.field, catalog)
        else:
            values = [str(x) for x in (v or [])]
            left = _as_text(raw)
        if not values:
            return {"$literal": p == "notin"}
        member = {"$in": [left, {"$literal": values}]}
        # notin excludes nulls, matching pandas' ``~m & s.notnull()``.
        return member if p == "in" else {"$and": [not_null(raw), _false(member)]}

    left, lit = _comparable(t.field, ftype, v if p != "between" else v[0], catalog, raw)

    if p == "between":
        return {
            "$and": [
                not_null(raw),
                {"$gte": [left, lit(v[0])]},
                {"$lte": [left, lit(v[1])]},
            ]
        }
    if p == "eq":
        return {"$eq": [left, lit(v)]}
    if p == "neq":
        # pandas excludes nulls explicitly here, so null != x is False.
        return {"$and": [not_null(raw), {"$ne": [left, lit(v)]}]}
    op = {"gt": "$gt", "gte": "$gte", "lt": "$lt", "lte": "$lte"}[p]
    # Ordered comparisons must be guarded: BSON sorts null below every number,
    # so an unguarded ``null < 5`` would be true here and false everywhere else.
    return {"$and": [not_null(raw), {op: [left, lit(v)]}]}


def _comparable(
    fld: str, ftype: str, sample: Any, catalog: FieldCatalog, raw: Any
) -> Tuple[Any, Any]:
    """Coerce column and literal into one comparable domain, as pandas does."""
    if ftype == "temporal":
        return as_date(raw), (lambda x: as_date({"$literal": str(x)}))
    if ftype == "quantitative" or isinstance(sample, (int, float)) and not isinstance(sample, bool):
        return num_expr(fld, catalog), (lambda x: float(x))
    return _as_text(raw), (lambda x: {"$literal": str(x)})


def filters_expr(
    filters: Sequence[FilterTransform | FilterAnyTransform], catalog: FieldCatalog
) -> Optional[Dict[str, Any]]:
    terms: List[Dict[str, Any]] = []
    for item in filters:
        if isinstance(item, FilterAnyTransform):
            clauses = [
                {"$and": [_filter_expr(t, catalog) for t in clause]} for clause in item.clauses
            ]
            terms.append({"$or": clauses})
        else:
            terms.append(_filter_expr(item, catalog))
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else {"$and": terms}

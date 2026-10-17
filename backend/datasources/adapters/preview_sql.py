"""Shared preview SQL builders (substring ILIKE, NULLS LAST, bound params).

Used by Postgres pushdown and DuckDB file preview so filter/search/sort
semantics stay aligned with ``preview_service.apply_query``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .base import escape_like


def quote_ident(name: str) -> str:
    """Quote an identifier by doubling embedded quotes (never mangles names)."""
    return '"' + str(name).replace('"', '""') + '"'


def build_where(
    column_names: List[str],
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """WHERE clause + bind params. Unknown filter columns are dropped (same as
    the pandas path); needles are always bound, never interpolated."""
    known = set(column_names)
    clauses: List[str] = []
    params: Dict[str, str] = {}
    for i, (name, needle) in enumerate((filters or {}).items()):
        if name not in known or not needle:
            continue
        params[f"f{i}"] = f"%{escape_like(needle)}%"
        clauses.append(f"CAST({quote_ident(name)} AS TEXT) ILIKE :f{i} ESCAPE '\\'")
    if search:
        params["srch"] = f"%{escape_like(search)}%"
        ors = " OR ".join(
            f"CAST({quote_ident(c)} AS TEXT) ILIKE :srch ESCAPE '\\'" for c in column_names
        )
        if ors:
            clauses.append(f"({ors})")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def build_order_by(
    column_names: List[str],
    sort_by: Optional[str],
    sort_dir: str = "asc",
    stable_tiebreak: Optional[str] = None,
) -> str:
    parts: List[str] = []
    if sort_by and sort_by in set(column_names):
        direction = "DESC" if sort_dir == "desc" else "ASC"
        parts.append(f"{quote_ident(sort_by)} {direction} NULLS LAST")
    if stable_tiebreak:
        parts.append(stable_tiebreak)
    return (" ORDER BY " + ", ".join(parts)) if parts else ""


def build_select_sql(
    source_ref: str,
    column_names: List[str],
    *,
    offset: int,
    limit: int,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    stable_tiebreak: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    select_list = ", ".join(quote_ident(c) for c in column_names)
    where_sql, params = build_where(column_names, filters, search)
    order_sql = build_order_by(column_names, sort_by, sort_dir, stable_tiebreak)
    params.update({"limit": int(limit), "offset": int(offset)})
    sql = (
        f"SELECT {select_list} FROM {source_ref}{where_sql}{order_sql} LIMIT :limit OFFSET :offset"
    )
    return sql, params


def build_count_sql(
    source_ref: str,
    column_names: List[str],
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    where_sql, params = build_where(column_names, filters, search)
    return f"SELECT COUNT(*) FROM {source_ref}{where_sql}", params

"""
SQL engine for applying a FilterSpec to a Postgres source (full-table
pushdown). Mirrors ``filters/service.py`` — both emit the identical page
shape — and borrows the diff engine's structure: an inner SELECT over the
source (plus annotation LEFT JOINs) exposing display aliases ``d<j>`` and
flags ``_m`` (select match), ``_r<i>`` (per-rule), ``_grp`` (group), ``_ord``;
an outer query applies narrowing/ordering/pagination, and one aggregate pass
computes the summary.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextvars import ContextVar
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from agents.sql_agent import working_copies as wc
from datasources.sql_dialect import get_dialect
from filters.annotate_sql import AnnotationJoin, build_annotation_joins
from filters.predicate_pandas import UnknownColumn
from filters.predicate_sql import ParamGen, compile_where
from filters.presentation import PresentationBuilder
from filters.service import AnnotationCollision
from filters.spec import SEVERITY_ORDER, FilterSpec

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {sev: i for i, sev in enumerate(SEVERITY_ORDER)}
_ID_LIKE = ("id", "index", "key")
_SUMMARY_CACHE_MAX = 4
_summary_cache: "OrderedDict[Tuple, Dict[str, Any]]" = OrderedDict()


_ident_q: ContextVar[str] = ContextVar("filter_ident_quote", default='"')


def _quote(name: str) -> str:
    q = _ident_q.get()
    if q == "[":
        return "[" + str(name).replace("]", "]]") + "]"
    return q + str(name).replace(q, q + q) + q


# ------------------------------------------------------------------ identity


def detect_key_single(adapter, identity) -> Optional[str]:
    """Resolve the spec's RowIdentity against a live table: PK if it exists,
    else a unique+non-null id-like column. None => keyless (annotations off).
    SQL has no stable physical order, so 'positional' identity degrades to
    keyless rather than pretending row numbers are stable."""
    from api.sql_diff_service import _probe_unique  # noqa: PLC0415

    columns = [c.name for c in adapter.schema()]

    if identity.mode == "key":
        if identity.key in columns and _probe_unique(adapter, identity.key):
            return identity.key
        return None
    if identity.mode == "positional":
        return None

    # Catalog lookup only where the adapter names a real relation; file/DuckDB
    # relations have no schema-qualified identity to ask about.
    schema_name = getattr(adapter, "db_schema", None)
    table_name = getattr(adapter, "table", None)
    if adapter.is_table and schema_name and table_name:
        lookup = get_dialect(getattr(adapter, "dialect", None)).primary_key_query(
            schema_name, table_name
        )
        if lookup is not None:
            _, rows = adapter.run(*lookup)
            if len(rows) == 1 and rows[0][0] in columns:
                return rows[0][0]

    candidates = [c for c in columns if c.lower() in _ID_LIKE or c.lower().endswith("_id")]
    for col in candidates:
        if _probe_unique(adapter, col):
            return col
    return None


# ------------------------------------------------------------- query builders


def build_display(
    schema_cols: List[Any],
    spec: FilterSpec,
    joins: List[AnnotationJoin],
) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[str, str]]]:
    """Display columns (grid specs) + name -> (sql_expr, kind) resolver map.

    Base columns come first, then every declared annotation output. Outputs
    whose step was skipped/failed resolve to NULL literals so predicates over
    them are simply FALSE — same behavior as the pandas engine's all-null
    columns for failed steps.
    """
    joined_exprs: Dict[str, str] = {}
    for join in joins:
        joined_exprs.update(join.outputs)

    display: List[Dict[str, Any]] = []
    exprs: Dict[str, Tuple[str, str]] = {}
    for col in schema_cols:
        if col.name in exprs or wc.is_internal_column(col.name):
            continue
        exprs[col.name] = (f"t.{_quote(col.name)}", col.kind)
        display.append({"name": col.name, "dtype": col.db_type, "kind": col.kind})

    kind_of = {"number": "number", "string": "string", "bool": "bool"}
    for name, output in spec.annotation_outputs().items():
        if name in exprs:
            raise AnnotationCollision(name)
        kind = kind_of.get(output.dtype, "other")
        exprs[name] = (joined_exprs.get(name, "NULL"), kind)
        display.append({"name": name, "dtype": output.dtype, "kind": kind, "origin": "annotation"})
    return display, exprs


def build_filter_join_sql(
    source_ref: str,
    display: List[Dict[str, Any]],
    exprs: Dict[str, Tuple[str, str]],
    joins: List[AnnotationJoin],
    *,
    select_sql: str,
    rule_sqls: List[str],
    group_expr: Optional[str],
    order_expr: str,
    dialect: str = "postgres",
) -> str:
    d = get_dialect(dialect)
    items = [f"{exprs[c['name']][0]} AS d{j}" for j, c in enumerate(display)]
    # T-SQL has no boolean scalar type; every predicate projected by the inner
    # relation must be materialized as a dialect-specific 0/1 value.
    items.append(f"{d.select_bool(select_sql)} AS _m")
    items.extend(f"{d.select_bool(sql)} AS _r{i}" for i, sql in enumerate(rule_sqls))
    # SQL Server rejects COUNT(DISTINCT NULL) because an untyped NULL literal
    # has no operand type. Keep the absent-group sentinel explicitly typed.
    items.append(f"{group_expr or d.cast_text('NULL')} AS _grp")
    items.append(f"{order_expr} AS _ord")
    join_clause = " ".join(j.join_sql for j in joins)
    return f"SELECT {', '.join(items)} FROM {source_ref} t" + (
        f" {join_clause}" if join_clause else ""
    )


def build_filter_where(
    display: List[Dict[str, Any]],
    params: ParamGen,
    *,
    matches_only: bool,
    enabled_rule_idx: Optional[List[int]],
    n_rules: int,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    dialect: str = "postgres",
) -> str:
    from datasources.adapters.base import escape_like  # noqa: PLC0415

    d = get_dialect(dialect)
    clauses: List[str] = []
    if matches_only:
        clauses.append(d.test_bool("q._m"))
    if enabled_rule_idx is not None:
        ors = " OR ".join(d.test_bool(f"q._r{i}") for i in enabled_rule_idx if 0 <= i < n_rules)
        clauses.append(f"({ors})" if ors else d.test_bool(d.bool_literal(False)))

    names = {c["name"]: j for j, c in enumerate(display)}
    for name, needle in (filters or {}).items():
        j = names.get(name)
        if j is None or not needle:
            continue
        bound = params.add(f"%{escape_like(needle)}%")
        clauses.append(d.ilike(d.cast_text(f"q.d{j}"), bound))
    if search and display:
        bound = params.add(f"%{escape_like(search)}%")
        ors = " OR ".join(d.ilike(d.cast_text(f"q.d{j}"), bound) for j in range(len(display)))
        clauses.append(f"({ors})")

    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def build_filter_page_sql(
    inner: str,
    where_sql: str,
    display: List[Dict[str, Any]],
    params: ParamGen,
    *,
    grouping: bool,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    offset: int = 0,
    limit: int = 100,
    dialect: str = "postgres",
) -> str:
    d = get_dialect(dialect)
    if grouping:
        # Grouping owns the order: groups first (members adjacent), then the rest.
        order_parts = [f"{d.select_bool('q._grp IS NULL')} ASC", "q._grp", "q._ord"]
    else:
        order_parts = []
        names = {c["name"]: j for j, c in enumerate(display)}
        if sort_by and sort_by in names:
            order_parts.append(d.order_term(f"q.d{names[sort_by]}", sort_dir))
        order_parts.append("q._ord")
    limit_bound = params.add(int(limit))
    offset_bound = params.add(int(offset))
    return (
        f"SELECT q.* FROM ({inner}) q{where_sql} "
        f"ORDER BY {', '.join(order_parts)}{d.pagination_suffix(limit_bound, offset_bound)}"
    )


def build_filter_summary_sql(inner: str, n_rules: int, *, dialect: str = "postgres") -> str:
    d = get_dialect(dialect)
    items = [
        f"COUNT(*) AS {d.quote('total')}",
        f"{d.count_if(d.test_bool('q._m'))} AS {d.quote('matched')}",
        # GROUPS is a reserved word on BigQuery (window frames), so every output
        # alias is quoted rather than guessing which names are safe per engine.
        f"COUNT(DISTINCT q._grp) AS {d.quote('groups')}",
    ]
    items.extend(
        f"{d.count_if(d.test_bool(f'q._r{i}'))} AS {d.quote(f'r{i}')}" for i in range(n_rules)
    )
    return f"SELECT {', '.join(items)} FROM ({inner}) q"


# ------------------------------------------------------------------- serving


def build_sql_filter_page(
    adapter,
    spec: FilterSpec,
    *,
    session_id: str,
    page: int = 1,
    page_size: int = 100,
    matches_only: Optional[bool] = None,
    enabled_rules: Optional[List[str]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Serve one page of the source through the filter — full pushdown.

    Raises UnknownColumn when the select predicate references a missing
    column; rule-level unknowns skip that rule with a warning.
    """
    quote_token = _ident_q.set(getattr(adapter, "ident_quote", None) or '"')
    try:
        return _build_sql_filter_page_impl(
            adapter,
            spec,
            session_id=session_id,
            page=page,
            page_size=page_size,
            matches_only=matches_only,
            enabled_rules=enabled_rules,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
            filters=filters,
        )
    finally:
        _ident_q.reset(quote_token)


def _build_sql_filter_page_impl(
    adapter,
    spec: FilterSpec,
    *,
    session_id: str,
    page: int = 1,
    page_size: int = 100,
    matches_only: Optional[bool] = None,
    enabled_rules: Optional[List[str]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    from datasources.adapters.base import DIFF_TIMEOUT_MS, cached_count  # noqa: PLC0415
    from helpers.helper import to_json_safe  # noqa: PLC0415

    key = detect_key_single(adapter, spec.identity)
    identity_label = f"key:{key}" if key else "none"
    warnings: List[str] = []
    if spec.identity.mode == "key" and key is None:
        warnings.append(
            f"key column {spec.identity.key!r} is missing or not unique; annotations are disabled"
        )

    joins, statuses, degraded = build_annotation_joins(
        adapter, spec, session_id=session_id, source_key=key
    )
    schema_cols = adapter.schema()
    display, exprs = build_display(schema_cols, spec, joins)

    def resolver(name: str) -> Optional[Tuple[str, str]]:
        return exprs.get(name)

    params = ParamGen()
    dialect = getattr(adapter, "dialect", "postgres")
    d = get_dialect(dialect)
    select_sql = (
        compile_where(spec.select, resolver, params, dialect=dialect)
        if spec.select is not None
        else ("1=1" if d.name == "sqlserver" else d.bool_literal(True))
    )

    rule_ids: List[str] = []
    rule_sqls: List[str] = []
    for rule in spec.present.rules:
        if rule.predicate is None:
            rule_ids.append(rule.id)
            rule_sqls.append(select_sql)
            continue
        try:
            rule_sqls.append(compile_where(rule.predicate, resolver, params, dialect=dialect))
            rule_ids.append(rule.id)
        except UnknownColumn as exc:
            warnings.append(f"rule {rule.id!r} references unknown column {exc.column!r}")

    group_by = spec.present.group_by
    group_expr = None
    if group_by:
        expr, _kind = exprs.get(group_by, ("NULL", "other"))
        group_expr = None if expr == "NULL" else expr
    grouping = group_expr is not None

    if key:
        order_expr = d.cast_text(f"t.{_quote(key)}")
    else:
        # No user key: fall back to whatever stable row address the engine has.
        order_expr = d.stable_order_expr(is_table=bool(getattr(adapter, "is_table", False)))
        if order_expr is None:
            order_expr = "NULL"
        elif d.row_address:
            order_expr = d.cast_text(f"t.{d.row_address}")
    inner = build_filter_join_sql(
        adapter.source_ref,
        display,
        exprs,
        joins,
        select_sql=select_sql,
        rule_sqls=rule_sqls,
        group_expr=group_expr,
        order_expr=order_expr,
        dialect=dialect,
    )

    effective_matches_only = spec.present.matches_only if matches_only is None else matches_only
    enabled_rule_idx = (
        None
        if enabled_rules is None
        else [i for i, rid in enumerate(rule_ids) if rid in enabled_rules]
    )
    # Distinct prefixes: predicate params (fp*) live inside `inner` and are
    # needed by every query; narrowing (w*) and pagination (pg*) bind on top.
    where_gen = ParamGen("w")
    where_sql = build_filter_where(
        display,
        where_gen,
        matches_only=effective_matches_only,
        enabled_rule_idx=enabled_rule_idx,
        n_rules=len(rule_sqls),
        filters=filters,
        search=search,
        dialect=dialect,
    )

    # ---- counts + summary (cached until the working table's generation bumps)
    info = adapter.cache_info
    total = cached_count(
        (session_id, *info.key, "flt", None, None), info.ttl, lambda: adapter.count(None, None)
    )

    summary_key = (
        session_id,
        *info.key,
        "flt-summary",
        spec.annotate_fingerprint(),
        select_sql,
        tuple(rule_sqls),
    )
    stats = _summary_cache.get(summary_key)
    if stats is None:
        _keys, rows = adapter.run(
            build_filter_summary_sql(inner, len(rule_sqls), dialect=dialect),
            params.values,
            timeout_ms=DIFF_TIMEOUT_MS,
        )
        row = rows[0] if rows else [0, 0, 0] + [0] * len(rule_sqls)
        stats = {
            "total": int(row[0] or 0),
            "matched": int(row[1] or 0),
            "groups": int(row[2] or 0),
            "rules": {rid: int(row[3 + i] or 0) for i, rid in enumerate(rule_ids)},
        }
        _summary_cache[summary_key] = stats
        while len(_summary_cache) > _SUMMARY_CACHE_MAX:
            _summary_cache.popitem(last=False)

    if where_sql:
        _keys, rows = adapter.run(
            f"SELECT COUNT(*) FROM ({inner}) q{where_sql}",
            {**params.values, **where_gen.values},
            timeout_ms=DIFF_TIMEOUT_MS,
        )
        filtered = int(rows[0][0] or 0) if rows else 0
    else:
        filtered = stats["total"]

    # ---- page ----
    total_pages = max(1, ceil(filtered / page_size))
    page = min(max(1, page), total_pages)
    page_gen = ParamGen("pg")
    page_sql = build_filter_page_sql(
        inner,
        where_sql,
        display,
        page_gen,
        grouping=grouping,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=(page - 1) * page_size,
        limit=page_size,
        dialect=dialect,
    )
    _keys, raw_rows = adapter.run(
        page_sql,
        {**params.values, **where_gen.values, **page_gen.values},
        timeout_ms=DIFF_TIMEOUT_MS,
    )

    n_display = len(display)
    m_idx = n_display
    r_base = n_display + 1
    grp_idx = r_base + len(rule_sqls)

    display_names = [c["name"] for c in display]
    builder = PresentationBuilder(display_names, spec.present.rules)
    rows_out: List[List[Any]] = []

    for raw in raw_rows:
        values = [to_json_safe(v) for v in raw[:n_display]]
        rows_out.append(values)
        builder.add_row(
            values,
            matched=bool(raw[m_idx]),
            fired_rule_ids=[rid for i, rid in enumerate(rule_ids) if raw[r_base + i]],
            group_value=raw[grp_idx],
        )
    cell_meta = builder.cell_meta
    row_meta = builder.row_meta

    summary = {
        "matched_rows": stats["matched"],
        "total_rows": total.value,
        "approx": bool(total.approx) or degraded == "fuzzy_row_cap",
        "rule_counts": stats["rules"],
        "group_count": stats["groups"] if grouping else None,
        "identity_resolved": identity_label,
        "degraded": degraded,
        "warning": "; ".join(warnings) if warnings else None,
        "annotations": statuses,
        "group_by": group_by if grouping else None,
    }
    return {
        "kind": "filter",
        "columns": display,
        "rows": rows_out,
        "cell_meta": cell_meta,
        "row_meta": row_meta,
        "total_rows": total.value,
        "filtered_rows": filtered,
        "page": page,
        "total_pages": total_pages,
        "summary": summary,
    }


def clear_caches() -> None:
    """Test helper."""
    _summary_cache.clear()

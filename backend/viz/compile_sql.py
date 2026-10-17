"""
SQL (Postgres) execution of a QueryPlan — full-table aggregation pushdown.

Renders the SAME semantics as ``viz.compile_pandas`` (parity-tested): null
dimension keys dropped, ``date_trunc`` period starts emitted as ISO strings,
equal-width bins with the last edge clamped, topK ranked by the first measure
with an optional re-aggregated "Other" bucket, floats rounded to 6 decimals.

Produces a single read-only statement:

    WITH src AS (filtered source),
         [stats AS (bin min/max)],
         keyed AS (derived dimension keys + measure inputs, null keys dropped),
         [top_<ch> AS (top-n dimension values)]
    SELECT ... GROUP BY ... ORDER BY ... LIMIT cap

The caller executes it via ``sql_agent.run_sql`` (SELECT/WITH-only enforced
there). Values are inlined as escaped literals because ``run_sql`` takes no
bind parameters; every literal passes through ``_lit`` below.
"""

from __future__ import annotations

from typing import Any, List, Optional

from datasources.sql_dialect import current_dialect, get_dialect, use_dialect

from .catalog import PG_NUMERIC_TYPES, FieldCatalog
from .expr import materialize_sql
from .plan import PlannedDim, PlannedMeasure, QueryPlan
from .spec import FilterAnyTransform, FilterTransform

OTHERS_LABEL = "Other (remaining)"


def _d():
    """Dialect for the statement being compiled."""
    return current_dialect()


def _q(name: str) -> str:
    return _d().quote(name)


def _lit(v: Any) -> str:
    """Escaped SQL literal for a filter value."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    s = str(v)
    if "\x00" in s:
        raise ValueError("Filter value contains NUL byte")
    return "'" + s.replace("'", "''") + "'"


def _like_pattern(v: Any) -> str:
    s = str(v).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return _lit(f"%{s}%")


def compile_spec_sql(
    plan: QueryPlan,
    catalog: FieldCatalog,
    schema: str,
    table: str,
    *,
    dialect: str = "postgres",
    database: Optional[str] = None,
) -> str:
    """``catalog`` must include calculated columns (executor's effective catalog)."""
    with use_dialect(dialect):
        return _compile_spec_sql(plan, catalog, schema, table, dialect=dialect, database=database)


def _compile_spec_sql(
    plan: QueryPlan,
    catalog: FieldCatalog,
    schema: str,
    table: str,
    *,
    dialect: str,
    database: Optional[str],
) -> str:
    src_table = get_dialect(dialect).relation(schema, table, database=database)
    where = _filters_sql(plan.filters, catalog)
    if plan.calcs:
        # Calculated columns project in a base CTE BEFORE filters, so filters
        # (and every downstream expression) see them as ordinary columns.
        calc_cols = ", ".join(
            f"{materialize_sql(c.ast, catalog)} AS {_q(c.name)}" for c in plan.calcs
        )
        ctes = [
            f"base AS (SELECT *, {calc_cols} FROM {src_table})",
            f"src AS (SELECT * FROM base{where})",
        ]
    else:
        ctes = [f"src AS (SELECT * FROM {src_table}{where})"]

    binned = [d for d in plan.dims if d.kind == "bin"]
    if binned:
        stats_cols = ", ".join(
            f"MIN({_num_expr(d.fld, catalog)}) AS {_q('mn_' + d.channel)}, "
            f"MAX({_num_expr(d.fld, catalog)}) AS {_q('mx_' + d.channel)}"
            for d in binned
        )
        ctes.append(f"stats AS (SELECT {stats_cols} FROM src)")

    keyed_select: List[str] = []
    not_null: List[str] = []
    for d in plan.dims:
        expr = _dim_key_expr(d, catalog)
        keyed_select.append(f"{expr} AS {_q('k_' + d.channel)}")
        not_null.append(f"{expr} IS NOT NULL")
        if d.kind == "bin":
            keyed_select.append(f"{_bin_width_expr(d)} AS {_q('w_' + d.channel)}")
    for m in plan.measures:
        if m.aggregate == "count":
            continue
        expr = _q(str(m.fld)) if m.aggregate == "distinct" else _num_expr(str(m.fld), catalog)
        keyed_select.append(f"{expr} AS {_q('m_' + m.channel)}")
    for k, f, t in plan.extras:
        keyed_select.append(f"{_output_cast(_q(f), t, dialect=dialect)} AS {_q(k)}")

    if plan.aggregated:
        null_guard = f" WHERE {' AND '.join(not_null)}" if not_null else ""
    else:
        req = [
            f"{_dim_key_expr(d, catalog)} IS NOT NULL" for d in plan.dims if d.channel in ("x", "y")
        ]
        null_guard = f" WHERE {' AND '.join(req)}" if req else ""
    join_stats = " CROSS JOIN stats" if binned else ""
    ctes.append(f"keyed AS (SELECT {', '.join(keyed_select)} FROM src{join_stats}{null_guard})")

    ranked = [d for d in plan.dims if d.top_k is not None]
    for d in ranked:
        ctes.append(_topk_cte(d, plan))
    source = "keyed"
    if ranked:
        ctes.append(_bucketed_cte(plan, ranked))
        source = "bucketed"

    final = _final_select(plan, dialect=dialect, source=source)
    return "WITH " + ",\n".join(ctes) + "\n" + final


def compile_spec_duckdb(plan: QueryPlan, catalog: FieldCatalog, relation: str) -> str:
    """Compile the shared plan for a validated, session-local DuckDB relation."""
    return compile_spec_sql(plan, catalog, "main", relation, dialect="duckdb")


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


def _db_type(fld: str, catalog: FieldCatalog) -> str:
    info = catalog.get(fld)
    return (info.db_type or "") if info is not None else ""


def _num_expr(fld: str, catalog: FieldCatalog) -> str:
    """Numeric-valued expression; text columns get a safe regex-guarded cast."""
    qc = _q(fld)
    db_type = _db_type(fld, catalog).lower()
    if db_type in PG_NUMERIC_TYPES or any(
        token in db_type
        for token in ("int", "numeric", "decimal", "real", "double", "float", "money")
    ):
        return qc
    return _d().numeric_guard(qc)


def _as_text(expr: str) -> str:
    return _d().cast_text(expr)


def _as_ts(expr: str) -> str:
    return _d().cast_timestamp(expr)


def _as_float(expr: str) -> str:
    return _d().cast_float(expr)


def _bin_width_expr(d: PlannedDim) -> str:
    mn, mx = _q("mn_" + d.channel), _q("mx_" + d.channel)
    return _as_float(f"(CASE WHEN {mx} = {mn} THEN 1.0 ELSE ({mx} - {mn}) / {d.maxbins} END)")


def _dim_key_expr(d: PlannedDim, catalog: FieldCatalog) -> str:
    qc = _q(d.fld)
    if d.kind == "time":
        return _d().date_trunc(str(d.time_unit), _as_ts(qc))
    if d.kind == "bin":
        v = _num_expr(d.fld, catalog)
        mn, mx = _q("mn_" + d.channel), _q("mx_" + d.channel)
        w = f"(({mx} - {mn}) / {d.maxbins})"
        capped = _d().least(f"FLOOR(({v} - {mn}) / {w})", f"{d.maxbins} - 1")
        start = f"{mn} + {capped} * {w}"
        return f"(CASE WHEN {v} IS NULL THEN NULL WHEN {mx} = {mn} THEN {mn} ELSE {start} END)"
    if d.field_type == "temporal":
        return _as_ts(qc)
    if d.field_type == "quantitative":
        return _num_expr(d.fld, catalog)
    return _as_text(qc)


def _output_cast(expr: str, field_type: str, *, dialect: str = "postgres") -> str:
    """Format a keyed value for the output row (ISO timestamps, rounded floats)."""
    d = get_dialect(dialect)
    if field_type == "temporal":
        return d.format_iso_timestamp(expr)
    if field_type == "quantitative":
        return d.round_float(expr)
    return d.cast_text(expr)


def _dim_output_expr(d: PlannedDim, key: str, *, dialect: str = "postgres") -> str:
    sql_dialect = get_dialect(dialect)
    if d.kind == "time":
        # The key is already a timestamp from date_trunc; Postgres formats it
        # without a re-cast, so keep that shape rather than round-tripping.
        return sql_dialect.format_iso_timestamp(key)
    if d.kind == "bin":
        return sql_dialect.round_float(key)
    return _output_cast(key, d.field_type, dialect=dialect)


def _agg_call(aggregate: str, src: str) -> str:
    """One aggregate over *src*.

    ``mean`` casts first: T-SQL's AVG over an integer column does integer
    division and silently truncates, where Postgres and pandas do not.
    """
    fn = {"sum": "SUM", "mean": "AVG", "min": "MIN", "max": "MAX"}[aggregate]
    return f"AVG({_d().cast_float(src)})" if aggregate == "mean" else f"{fn}({src})"


def _measure_output_expr(m: PlannedMeasure, *, dialect: str = "postgres") -> str:
    src = _q("m_" + m.channel)
    if m.aggregate == "count":
        return "COUNT(*)"
    if m.aggregate == "distinct":
        return f"COUNT(DISTINCT {src})"
    return get_dialect(dialect).round_float(_agg_call(m.aggregate, src))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _filter_sql(t: FilterTransform, catalog: FieldCatalog) -> str:
    qc = _q(t.field)
    info = catalog.get(t.field)
    ftype = info.type if info is not None else "nominal"
    p, v = t.predicate, t.value

    if p == "isnull":
        return f"{qc} IS NULL"
    if p == "notnull":
        return f"{qc} IS NOT NULL"
    if p == "contains":
        return _d().ilike(_as_text(qc), _like_pattern(v))
    if p in ("in", "notin"):
        op = "IN" if p == "in" else "NOT IN"
        if ftype == "quantitative":
            # Numeric membership compares numerically — float text formatting
            # differs between engines (pandas "5.0" vs Postgres float8 "5").
            nums = []
            for x in v or []:
                try:
                    nums.append(_lit(float(x)))
                except (TypeError, ValueError):
                    continue
            if not nums:
                return "FALSE" if p == "in" else "TRUE"
            return f"{_num_expr(t.field, catalog)} {op} ({', '.join(nums)})"
        vals = ", ".join(_lit(str(x)) for x in (v or []))
        return f"{_as_text(qc)} {op} ({vals})" if vals else ("FALSE" if p == "in" else "TRUE")

    if ftype == "temporal":
        left, lit = _as_ts(qc), lambda x: _as_ts(_lit(str(x)))
    elif ftype == "quantitative" or isinstance(v, (int, float)):
        left, lit = _num_expr(t.field, catalog), lambda x: _lit(float(x))
    else:
        left, lit = _as_text(qc), lambda x: _lit(str(x))

    if p == "between":
        return f"({left} >= {lit(v[0])} AND {left} <= {lit(v[1])})"
    op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[p]
    return f"{left} {op} {lit(v)}"


def _filters_sql(filters: List[FilterTransform | FilterAnyTransform], catalog: FieldCatalog) -> str:
    if not filters:
        return ""
    terms = []
    for item in filters:
        if isinstance(item, FilterAnyTransform):
            clauses = [
                "(" + " AND ".join(_filter_sql(t, catalog) for t in clause) + ")"
                for clause in item.clauses
            ]
            terms.append("(" + " OR ".join(clauses) + ")")
        else:
            terms.append(_filter_sql(item, catalog))
    return " WHERE " + " AND ".join(terms)


# ---------------------------------------------------------------------------
# topK + final select
# ---------------------------------------------------------------------------


def _rank_expr(plan: QueryPlan) -> str:
    first = plan.measures[0]
    if first.aggregate == "count":
        return "COUNT(*)"
    if first.aggregate == "distinct":
        return f"COUNT(DISTINCT {_q('m_' + first.channel)})"
    return _agg_call(first.aggregate, _q("m_" + first.channel))


def _topk_cte(d: PlannedDim, plan: QueryPlan) -> str:
    key = _q("k_" + d.channel)
    n, _ = d.top_k or (20, False)
    dialect = _d()
    order = ", ".join(
        (dialect.order_term(_rank_expr(plan), "desc"), dialect.order_term(key, "asc"))
    )
    return (
        f"{_q('top_' + d.channel)} AS (SELECT {dialect.select_prefix(n)}{key} FROM keyed "
        f"GROUP BY {key} ORDER BY {order}{dialect.limit_suffix(n)})"
    )


def _bucketed_cte(plan: QueryPlan, ranked: List[PlannedDim]) -> str:
    """Apply topK selection and the "Other" bucket before grouping.

    Doing this upstream keeps the final GROUP BY a list of plain columns, which
    SQL Server requires — it rejects both an output alias and a subquery there.
    """
    select: List[str] = []
    for d in plan.dims:
        key = _q("k_" + d.channel)
        select.append(f"{_final_dim_key(d)} AS {key}" if d.top_k is not None else key)
        if d.kind == "bin":
            select.append(_q("w_" + d.channel))
    for m in plan.measures:
        if m.aggregate != "count":
            select.append(_q("m_" + m.channel))
    for k, _f, _t in plan.extras:
        select.append(_q(k))

    where = []
    for d in ranked:
        if not d.top_k[1]:
            qk = _q("k_" + d.channel)
            where.append(f"{qk} IN (SELECT {qk} FROM {_q('top_' + d.channel)})")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return f"bucketed AS (SELECT {', '.join(select)} FROM keyed{clause})"


def _final_dim_key(d: PlannedDim) -> str:
    key = _q("k_" + d.channel)
    if d.top_k is None:
        return key
    _, keep_others = d.top_k
    in_top = f"{key} IN (SELECT {key} FROM {_q('top_' + d.channel)})"
    if keep_others:
        return f"(CASE WHEN {in_top} THEN {key} ELSE {_lit(OTHERS_LABEL)} END)"
    return key


def _final_select(plan: QueryPlan, *, dialect: str = "postgres", source: str = "keyed") -> str:
    select: List[str] = []
    group: List[str] = []
    where: List[str] = []
    hidden: List[str] = []

    expr_by_channel: dict = {}
    bucketed = source != "keyed"
    for d in plan.dims:
        # When a bucketed CTE ran, the key column already holds the final value.
        key = _q("k_" + d.channel) if bucketed else _final_dim_key(d)
        expr = _dim_output_expr(d, key, dialect=dialect)
        expr_by_channel[d.channel] = expr
        select.append(f"{expr} AS {_q(d.channel)}")
        # Group by the expression, not the output alias: Postgres and BigQuery
        # accept the alias, SQL Server does not, and the expression is valid
        # everywhere.
        group.append(expr)
        if not bucketed and d.top_k is not None and not d.top_k[1]:
            qk = _q("k_" + d.channel)
            where.append(f"{qk} IN (SELECT {qk} FROM {_q('top_' + d.channel)})")
        if d.kind == "bin":
            w = _q("w_" + d.channel)
            hidden.append(
                (f"MIN({w})" if plan.aggregated else w) + f" AS {_q('__bin_w_' + d.channel)}"
            )

    if plan.aggregated:
        for m in plan.measures:
            expr = _measure_output_expr(m, dialect=dialect)
            expr_by_channel[m.channel] = expr
            select.append(f"{expr} AS {_q(m.channel)}")
    else:
        for k, _f, _t in plan.extras:
            expr_by_channel[k] = _q(k)
            select.append(_q(k))

    d = get_dialect(dialect)
    if d.supports_nulls_ordering:
        order = ", ".join(d.order_term(_q(c), direction) for c, direction in plan.order)
    else:
        # An output alias cannot appear inside an ORDER BY expression in T-SQL,
        # so the null rank is projected as its own column and ordered by name.
        terms = []
        for channel, direction in plan.order:
            rank = _q("__null_" + channel)
            expression = expr_by_channel.get(channel, _q(channel))
            hidden.append(f"(CASE WHEN {expression} IS NULL THEN 1 ELSE 0 END) AS {rank}")
            terms.append(f"{rank} ASC")
            terms.append(f"{_q(channel)} {'ASC' if direction == 'asc' else 'DESC'}")
        order = ", ".join(terms)
    # One sentinel row so the caller can distinguish exactly-at-cap from truly
    # truncated output without a second COUNT query.
    cap = int(plan.row_cap) + 1
    sql = f"SELECT {d.select_prefix(cap)}{', '.join(select + hidden)} FROM {source}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if plan.aggregated and group:
        sql += " GROUP BY " + ", ".join(group)
    if order:
        sql += " ORDER BY " + order
    return sql + d.limit_suffix(cap)


def extract_hidden_meta(records: List[dict], plan: QueryPlan) -> Optional[dict]:
    """Pop ``__bin_w_*`` hidden columns from result records into bins meta."""
    bins: dict = {}
    for d in plan.dims:
        if d.kind != "bin":
            continue
        col = f"__bin_w_{d.channel}"
        width = records[0].get(col) if records else None
        for r in records:
            r.pop(col, None)
        if width is not None:
            bins[d.channel] = {"width": float(width)}
    return bins or None

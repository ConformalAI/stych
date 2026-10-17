"""
SQL compiler for the FilterSpec select stage (Postgres and DuckDB dialects).

Mirrors ``predicate_pandas`` semantics exactly: predicates are *total*
booleans — a comparison against a NULL cell is FALSE (COALESCE-wrapped), so
``not``/``and``/``or`` behave like pandas mask complement/combination, and
``ne`` is IS DISTINCT FROM (a null cell IS distinct from a non-null literal).

Every literal is bound (:fp0, :fp1, …) — never interpolated. The resolver maps
a column name to its aliased SQL expression + kind (e.g. ('t."amount"',
'number')); unknown names raise UnknownColumn, which callers turn into a 400
(select) or a skipped rule.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from datasources.sql_dialect import get_dialect
from filters.predicate_pandas import UnknownColumn
from filters.spec import (
    BetweenPred,
    BoolPred,
    ColumnOperand,
    ComparePred,
    MembershipPred,
    NotPred,
    NullPred,
    StringPred,
)

# resolver: column name -> (sql_expr, kind) — kind is the grid's column kind.
SqlResolver = Callable[[str], Optional[Tuple[str, str]]]

_ORDER_OPS = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


class ParamGen:
    """Sequential bind-parameter allocator shared across one query build."""

    def __init__(self, prefix: str = "fp") -> None:
        self.prefix = prefix
        self.values: Dict[str, Any] = {}

    def add(self, value: Any) -> str:
        name = f"{self.prefix}{len(self.values)}"
        self.values[name] = value
        return f":{name}"


def _resolve(resolver: SqlResolver, name: str) -> Tuple[str, str]:
    resolved = resolver(name)
    if resolved is None:
        raise UnknownColumn(name)
    return resolved


def _coerced_expr(expr: str, kind: str, literal: Any, *, dialect: str = "postgres") -> str:
    """Align the column expression with the literal's type, mirroring the
    pandas compiler's best-effort coercion. A numeric literal against a text
    column casts the column to numeric (unparseable rows error loudly — the
    agent sees the schema before authoring, so this is a spec bug surfaced,
    not silent misbehavior)."""
    if isinstance(literal, bool):
        return expr
    d = get_dialect(dialect)
    if isinstance(literal, (int, float)) and kind != "number":
        return d.cast_numeric(expr)
    if isinstance(literal, str) and kind not in ("string", "date"):
        return d.cast_text(expr)
    return expr


def _escape_like(needle: str) -> str:
    from datasources.adapters.base import escape_like  # noqa: PLC0415 — avoid heavy import chain

    return escape_like(needle)


def _total_predicate(predicate: str, dialect: str) -> str:
    """Turn SQL's three-valued predicate into the filter's total boolean.

    SQL Server has no boolean scalar type, so ``COALESCE(predicate, FALSE)``
    is not legal T-SQL.  A CASE expression remains a predicate when compared
    with one, and gives NULL comparisons the same false result as pandas.
    Other dialects retain the established Postgres-shaped expression.
    """
    d = get_dialect(dialect)
    if d.name == "sqlserver":
        return f"(CASE WHEN ({predicate}) THEN 1 ELSE 0 END = 1)"
    return f"COALESCE({predicate}, FALSE)"


def compile_where(
    pred, resolver: SqlResolver, params: ParamGen, *, dialect: str = "postgres"
) -> str:
    """Compile a predicate tree to a total-boolean SQL expression."""
    d = get_dialect(dialect)
    if isinstance(pred, BoolPred):
        joiner = " AND " if pred.op == "and" else " OR "
        return (
            "("
            + joiner.join(
                compile_where(c, resolver, params, dialect=dialect) for c in pred.children
            )
            + ")"
        )
    if isinstance(pred, NotPred):
        return f"(NOT {compile_where(pred.child, resolver, params, dialect=dialect)})"

    expr, kind = _resolve(resolver, pred.column)

    if isinstance(pred, NullPred):
        return f"({expr} IS NULL)" if pred.op == "is_null" else f"({expr} IS NOT NULL)"

    if isinstance(pred, ComparePred):
        if isinstance(pred.value, ColumnOperand):
            other, _other_kind = _resolve(resolver, pred.value.column)
            if pred.op == "ne":
                # Distinct, except the both-null pair (mirrors pandas).
                return d.distinct_from(expr, other)
            return _total_predicate(f"{expr} {_ORDER_OPS[pred.op]} {other}", dialect)
        bound = params.add(pred.value)
        lhs = _coerced_expr(expr, kind, pred.value, dialect=dialect)
        if pred.op == "ne":
            return d.distinct_from(lhs, bound)
        return _total_predicate(f"{lhs} {_ORDER_OPS[pred.op]} {bound}", dialect)

    if isinstance(pred, BetweenPred):

        def side(operand, op: str) -> str:
            if isinstance(operand, ColumnOperand):
                other, _ = _resolve(resolver, operand.column)
                return _total_predicate(f"{expr} {op} {other}", dialect)
            bound = params.add(operand)
            lhs = _coerced_expr(expr, kind, operand, dialect=dialect)
            return _total_predicate(f"{lhs} {op} {bound}", dialect)

        return f"({side(pred.low, '>=')} AND {side(pred.high, '<=')})"

    if isinstance(pred, StringPred):
        d = get_dialect(dialect)
        text_expr = d.cast_text(expr)
        if pred.op == "regex":
            bound = params.add(pred.pattern)
            match = d.regex_match(text_expr, bound, case_sensitive=pred.case_sensitive)
            return _total_predicate(match, dialect)
        escaped = _escape_like(pred.pattern)
        needle = {
            "contains": f"%{escaped}%",
            "startswith": f"{escaped}%",
            "endswith": f"%{escaped}",
        }[pred.op]
        bound = params.add(needle)
        if pred.case_sensitive:
            match = d.like(text_expr, bound)
        else:
            match = d.ilike(text_expr, bound)
        return _total_predicate(match, dialect)

    if isinstance(pred, MembershipPred):
        values = [v for v in pred.values if v is not None]
        if not values:
            return (
                d.test_bool(d.bool_literal(False)) if pred.op == "in" else f"({expr} IS NOT NULL)"
            )
        lhs = _coerced_expr(expr, kind, values[0], dialect=dialect)
        bounds = ", ".join(params.add(v) for v in values)
        keyword = "IN" if pred.op == "in" else "NOT IN"
        return _total_predicate(f"{lhs} {keyword} ({bounds})", dialect)

    raise TypeError(f"unsupported predicate node: {type(pred).__name__}")

"""Compile the FilterSpec select stage into a MongoDB ``$expr`` tree.

The third compiler for the same predicate tree, alongside ``predicate_pandas``
and ``predicate_sql``. It must agree with them on the one thing that is easy to
get wrong: **null semantics**. Both existing engines are *total* — a predicate
over a missing value is ``FALSE``, never null-propagating and never an error —
so this one is too.

``$expr`` is used rather than the terser query-operator form (``{age: {$gt: 5}}``)
because only ``$expr`` can compare two fields to each other, which
``ColumnOperand`` allows. One shape for every predicate beats two shapes with a
seam between them.

Documents are addressed by dotted path (``address.city``), matching how
``flatten_documents`` names flattened columns, so a column name from the catalog
maps to a field path unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Union

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

# Mongo comparison operators, keyed by the spec's op names.
_COMPARE = {"eq": "$eq", "ne": "$ne", "gt": "$gt", "ge": "$gte", "lt": "$lt", "le": "$lte"}


class UnsupportedMongoPredicate(ValueError):
    """The predicate has no faithful pipeline equivalent."""


def field_path(column: str) -> str:
    """A column name as an aggregation field reference."""
    return f"${column}"


def _value(column: str) -> Dict[str, Any]:
    """The field's value, with a missing field normalized to null.

    Being explicit beats relying on the implicit missing-compares-as-null rule:
    it states the intended semantics at every use, and a raw missing path is
    exactly where engines and their stand-ins diverge.
    """
    return {"$ifNull": [field_path(column), None]}


def _operand(value: Any, known: set[str]) -> Any:
    """A literal, or a reference to another field."""
    if isinstance(value, ColumnOperand):
        if value.column not in known:
            raise UnknownColumn(value.column)
        return _value(value.column)
    return {"$literal": value}


def _present(column: str) -> Dict[str, Any]:
    """True when the field exists and is not null.

    In an aggregation expression a missing field compares equal to null, so one
    check covers both — which is what the pandas and SQL engines mean by null.
    """
    return {"$ne": [_value(column), None]}


def _negate(expression: Dict[str, Any]) -> Dict[str, Any]:
    """Logical NOT, without using ``$not``.

    Every expression this module builds is *total* — it evaluates to true or
    false and never to null — so ``x == false`` is exactly ``NOT x``. The
    ``$not`` operator is documented as taking a one-element array, but
    implementations disagree about the bare form, and picking wrong would
    silently invert a filter rather than fail. Equality has no such ambiguity.
    """
    return {"$eq": [expression, False]}


def _guarded(column: str, expression: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate *expression* only where the field is present, else FALSE.

    Without this, comparing a missing field against a number returns TRUE for
    some Mongo type-ordering cases — the engine sorts BSON types rather than
    refusing to compare them, which would silently disagree with pandas and SQL.
    """
    return {"$cond": {"if": _present(column), "then": expression, "else": False}}


def _regex_match(column: str, pattern: str, *, case_sensitive: bool) -> Dict[str, Any]:
    options = "" if case_sensitive else "i"
    return _guarded(
        column,
        {
            "$regexMatch": {
                "input": {"$toString": _value(column)},
                "regex": pattern,
                "options": options,
            }
        },
    )


def _escape_regex(value: str) -> str:
    import re  # noqa: PLC0415

    return re.escape(value)


def compile_match(
    pred: Union[ComparePred, BetweenPred, StringPred, MembershipPred, NullPred, BoolPred, NotPred],
    known_columns: set[str],
) -> Dict[str, Any]:
    """Predicate tree -> a boolean aggregation expression."""
    if isinstance(pred, BoolPred):
        op = "$and" if pred.op == "and" else "$or"
        return {op: [compile_match(child, known_columns) for child in pred.children]}

    if isinstance(pred, NotPred):
        # NOT of a total predicate stays total.
        return _negate(compile_match(pred.child, known_columns))

    column = getattr(pred, "column", None)
    if column is not None and column not in known_columns:
        raise UnknownColumn(column)

    if isinstance(pred, NullPred):
        present = _present(column)
        return present if pred.op == "not_null" else _negate(present)

    if isinstance(pred, ComparePred):
        rhs = _operand(pred.value, known_columns)
        comparison = {_COMPARE[pred.op]: [_value(column), rhs]}
        if isinstance(pred.value, ColumnOperand):
            other = pred.value.column
            if pred.op == "eq":
                # pandas/SQL: null == null is false for column pairs.
                return {
                    "$and": [_present(column), _present(other), comparison],
                }
            if pred.op == "ne":
                # IS DISTINCT FROM: null vs null is false; one-null is true.
                return {_COMPARE["ne"]: [_value(column), rhs]}
            return {
                "$cond": {
                    "if": {"$and": [_present(column), _present(other)]},
                    "then": comparison,
                    "else": False,
                }
            }
        if pred.op in ("eq", "ne"):
            # Equality is null-safe in both existing engines: pandas follows
            # numpy (NaN != x is True) and SQL uses IS DISTINCT FROM. A missing
            # value *is* distinct from a literal, so no presence guard here.
            return comparison
        # Ordered comparisons do need it. Mongo sorts BSON types rather than
        # refusing to compare across them, so null < 10 is true — which would
        # disagree with both other engines.
        return _guarded(column, comparison)

    if isinstance(pred, BetweenPred):
        low = _operand(pred.low, known_columns)
        high = _operand(pred.high, known_columns)
        return _guarded(
            column,
            {
                "$and": [
                    {"$gte": [_value(column), low]},
                    {"$lte": [_value(column), high]},
                ]
            },
        )

    if isinstance(pred, MembershipPred):
        values = [v for v in pred.values]
        inside = _guarded(column, {"$in": [_value(column), {"$literal": values}]})
        if pred.op == "in":
            return inside
        # not_in stays total: a missing field is FALSE, not TRUE.
        return _guarded(column, _negate({"$in": [_value(column), {"$literal": values}]}))

    if isinstance(pred, StringPred):
        if pred.op == "regex":
            return _regex_match(column, pred.pattern, case_sensitive=pred.case_sensitive)
        escaped = _escape_regex(pred.pattern)
        anchored = {
            "contains": escaped,
            "startswith": f"^{escaped}",
            "endswith": f"{escaped}$",
        }[pred.op]
        return _regex_match(column, anchored, case_sensitive=pred.case_sensitive)

    raise UnsupportedMongoPredicate(f"Unsupported predicate: {type(pred).__name__}")


def match_stage(pred: Any, known_columns: set[str]) -> Dict[str, Any]:
    """A ``$match`` stage for *pred*, ready to drop into a pipeline."""
    return {"$match": {"$expr": compile_match(pred, known_columns)}}


def projection(columns: List[str], *, include_id: bool = True) -> Dict[str, Any]:
    """A ``$project`` stage keeping *columns* (and ``_id``, the row identity)."""
    spec: Dict[str, Any] = {column: 1 for column in columns}
    spec["_id"] = 1 if include_id else 0
    return {"$project": spec}

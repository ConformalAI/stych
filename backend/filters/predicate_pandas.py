"""
Pandas compiler for the FilterSpec select stage.

``compile_mask`` turns a predicate tree into a boolean Series aligned to the
frame index. Semantics are *total*: a comparison against a null cell is False
(never null), and ``not`` is plain complement. The SQL compiler mirrors this
by COALESCE-ing sub-predicates to FALSE, so both engines agree on rows like
``NOT (amount > 5)`` when ``amount`` is null.

``ne`` follows SQL's IS DISTINCT FROM: a null cell *is* distinct from any
non-null literal, so it matches.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

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

Resolver = Callable[[str], pd.Series]


class UnknownColumn(KeyError):
    """Predicate references a column that exists in neither the base frame
    nor the declared annotation outputs."""

    def __init__(self, column: str) -> None:
        super().__init__(column)
        self.column = column


def _operand_series(operand, resolve: Resolver):
    """Returns (series_or_None, scalar_or_None) for an operand."""
    if isinstance(operand, ColumnOperand):
        return resolve(operand.column), None
    return None, operand


def _coerce_for_compare(series: pd.Series, value):
    """Best-effort dtype alignment so ordering comparisons don't TypeError.

    Numeric literal vs object column -> coerce column to numeric; string
    literal vs datetime column -> parse the literal. Unparseable cells become
    NaN and simply don't match, mirroring SQL's silent cast failures on text.
    """
    if pd.api.types.is_datetime64_any_dtype(series) and isinstance(value, str):
        return series, pd.to_datetime(value, errors="coerce")
    if isinstance(value, (int, float)) and not pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce"), value
    if isinstance(value, str) and not (
        pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)
    ):
        return series.astype(str), value
    return series, value


def _ordered(series: pd.Series, other, op: str) -> pd.Series:
    if isinstance(other, pd.Series):
        left, right = series, other
        valid = left.notna() & right.notna()
    else:
        left, right = _coerce_for_compare(series, other)
        if right is None or (isinstance(right, float) and pd.isna(right)):
            return pd.Series(False, index=series.index)
        valid = left.notna()
    try:
        if op == "gt":
            raw = left > right
        elif op == "ge":
            raw = left >= right
        elif op == "lt":
            raw = left < right
        else:
            raw = left <= right
    except TypeError:
        return pd.Series(False, index=series.index)
    return (raw & valid).fillna(False).astype(bool)


def _equals(series: pd.Series, other) -> pd.Series:
    if isinstance(other, pd.Series):
        raw = series == other
        valid = series.notna() & other.notna()
    else:
        left, right = _coerce_for_compare(series, other)
        raw = left == right
        valid = left.notna()
    return (raw & valid).fillna(False).astype(bool)


def _string_accessor(series: pd.Series) -> pd.Series:
    # Stringify non-null cells (mirrors SQL's CAST(col AS TEXT)) so string ops
    # match mixed/object columns; nulls are preserved (astype(str) would turn
    # NaN into a matchable "nan").
    return series.where(series.isna(), series.astype(str))


def compile_mask(pred, resolve: Resolver) -> pd.Series:
    """Compile a predicate tree to a boolean mask. Raises UnknownColumn."""
    if isinstance(pred, BoolPred):
        masks = [compile_mask(child, resolve) for child in pred.children]
        out = masks[0]
        for mask in masks[1:]:
            out = (out & mask) if pred.op == "and" else (out | mask)
        return out
    if isinstance(pred, NotPred):
        return ~compile_mask(pred.child, resolve)

    series = resolve(pred.column)

    if isinstance(pred, NullPred):
        return series.isna() if pred.op == "is_null" else series.notna()

    if isinstance(pred, ComparePred):
        other, scalar = _operand_series(pred.value, resolve)
        target = other if other is not None else scalar
        if pred.op == "eq":
            return _equals(series, target)
        if pred.op == "ne":
            # IS DISTINCT FROM: null vs non-null literal is distinct (matches);
            # null vs null column pair is not.
            eq = _equals(series, target)
            if isinstance(target, pd.Series):
                both_null = series.isna() & target.isna()
                return ~(eq | both_null)
            return ~eq
        return _ordered(series, target, pred.op)

    if isinstance(pred, BetweenPred):
        low_s, low = _operand_series(pred.low, resolve)
        high_s, high = _operand_series(pred.high, resolve)
        lo = _ordered(series, low_s if low_s is not None else low, "ge")
        hi = _ordered(series, high_s if high_s is not None else high, "le")
        return lo & hi

    if isinstance(pred, StringPred):
        text = _string_accessor(series)
        if pred.op == "regex":
            return text.str.contains(
                pred.pattern, case=pred.case_sensitive, regex=True, na=False
            ).astype(bool)
        if pred.op == "contains":
            return text.str.contains(
                pred.pattern, case=pred.case_sensitive, regex=False, na=False
            ).astype(bool)
        haystack = text if pred.case_sensitive else text.str.lower()
        needle = pred.pattern if pred.case_sensitive else pred.pattern.lower()
        method = haystack.str.startswith if pred.op == "startswith" else haystack.str.endswith
        return method(needle).fillna(False).astype(bool)

    if isinstance(pred, MembershipPred):
        values = [v for v in pred.values if v is not None]
        hit = series.isin(values) & series.notna()
        return hit if pred.op == "in" else (series.notna() & ~hit)

    raise TypeError(f"unsupported predicate node: {type(pred).__name__}")

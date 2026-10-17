"""
Calculated-field expression engine.

A small, whitelisted expression language for ``{"op": "calculate"}`` transform
entries. One grammar, one AST, two emitters — pandas and SQL — kept
semantically identical (parity-tested) exactly like the two spec compilers:

- fields by name (``revenue``) or bracketed for special characters
  (``[unit price]``); literals ``1.5``, ``'text'``, ``true``, ``null``
- arithmetic ``+ - * /`` (division is NULL-safe: ``x / 0`` → null in BOTH
  engines), comparisons ``= == != <> > >= < <=``, logic ``and or not``
- functions: ``if(cond, a, b)``, ``coalesce(a, b, …)``, ``abs(x)``,
  ``round(x[, digits])`` (half away from zero, like Postgres), ``floor(x)``,
  ``ceil(x)``, ``lower(s)``, ``upper(s)``, ``length(s)``

Boolean results materialize as ``'true'``/``'false'`` text (matching Postgres
``boolean::text``) so they group identically in both engines. Calculated
columns are projected BEFORE filters run, so filters and every channel can use
them exactly like real columns.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .catalog import FieldCatalog, FieldInfo
from .spec import CalculateTransform, SpecError

MAX_CALC_NAME_LEN = 64
MAX_EXPR_LEN = 1000

# result-type lattice values: quantitative | nominal | temporal | boolean | null
_CALC_DB_TYPE = {"quantitative": "double precision", "nominal": "text", "temporal": "timestamp"}


class ExprError(Exception):
    """Parse or type error inside an expression; ``pos`` is a 0-based offset."""

    def __init__(self, message: str, pos: int = 0, unknown_field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.unknown_field = unknown_field


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass
class Node:
    pos: int = dc_field(default=0, kw_only=True)
    # Result type, annotated by infer_type; emitters read it.
    type_: str = dc_field(default="", kw_only=True)


@dataclass
class Lit(Node):
    value: Any = None


@dataclass
class FieldRef(Node):
    name: str = ""


@dataclass
class Unary(Node):
    op: str = ""  # '-' | 'not'
    operand: Optional[Node] = None


@dataclass
class Binary(Node):
    op: str = ""  # + - * / == != > >= < <= and or
    left: Optional[Node] = None
    right: Optional[Node] = None


@dataclass
class Func(Node):
    name: str = ""
    args: List[Node] = dc_field(default_factory=list)


# name → (min_args, max_args)
_FUNCTIONS: Dict[str, Tuple[int, int]] = {
    "if": (3, 3),
    "coalesce": (2, 10),
    "abs": (1, 1),
    "round": (1, 2),
    "floor": (1, 1),
    "ceil": (1, 1),
    "lower": (1, 1),
    "upper": (1, 1),
    "length": (1, 1),
}

_KEYWORDS = {"and", "or", "not", "true", "false", "null"}


# ---------------------------------------------------------------------------
# Tokenizer + parser
# ---------------------------------------------------------------------------


@dataclass
class _Tok:
    kind: str  # num | str | ident | field | op | lparen | rparen | comma | end
    value: str
    pos: int


def _tokenize(src: str) -> List[_Tok]:
    toks: List[_Tok] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isdigit() or src[j] == "."):
                j += 1
            num = src[i:j]
            if num.count(".") > 1:
                raise ExprError(f"Invalid number '{num}'", i)
            toks.append(_Tok("num", num, i))
            i = j
            continue
        if c == "'":
            j, buf = i + 1, []
            while j < n:
                if src[j] == "'":
                    if j + 1 < n and src[j + 1] == "'":  # '' escapes a quote
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            if j >= n:
                raise ExprError("Unterminated string literal", i)
            toks.append(_Tok("str", "".join(buf), i))
            i = j + 1
            continue
        if c == "[":
            j = src.find("]", i + 1)
            if j < 0:
                raise ExprError("Unterminated [field] reference", i)
            name = src[i + 1 : j].strip()
            if not name:
                raise ExprError("Empty [field] reference", i)
            toks.append(_Tok("field", name, i))
            i = j + 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            toks.append(_Tok("ident", src[i:j], i))
            i = j
            continue
        two = src[i : i + 2]
        if two in ("==", "!=", ">=", "<=", "<>"):
            toks.append(
                _Tok("op", "==" if two == "==" else ("!=" if two in ("!=", "<>") else two), i)
            )
            i += 2
            continue
        if c in "+-*/=<>":
            toks.append(_Tok("op", "==" if c == "=" else c, i))
            i += 1
            continue
        if c == "(":
            toks.append(_Tok("lparen", c, i))
            i += 1
            continue
        if c == ")":
            toks.append(_Tok("rparen", c, i))
            i += 1
            continue
        if c == ",":
            toks.append(_Tok("comma", c, i))
            i += 1
            continue
        raise ExprError(f"Unexpected character {c!r}", i)
    toks.append(_Tok("end", "", n))
    return toks


class _Parser:
    def __init__(self, toks: List[_Tok]):
        self.toks = toks
        self.i = 0

    def peek(self) -> _Tok:
        return self.toks[self.i]

    def next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind: str, what: str) -> _Tok:
        t = self.next()
        if t.kind != kind:
            raise ExprError(f"Expected {what}, got {t.value or 'end of expression'!r}", t.pos)
        return t

    def parse(self) -> Node:
        node = self.or_()
        t = self.peek()
        if t.kind != "end":
            raise ExprError(f"Unexpected {t.value!r}", t.pos)
        return node

    def or_(self) -> Node:
        node = self.and_()
        while self._kw("or"):
            t = self.next()
            node = Binary(op="or", left=node, right=self.and_(), pos=t.pos)
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self._kw("and"):
            t = self.next()
            node = Binary(op="and", left=node, right=self.not_(), pos=t.pos)
        return node

    def not_(self) -> Node:
        if self._kw("not"):
            t = self.next()
            return Unary(op="not", operand=self.not_(), pos=t.pos)
        return self.comparison()

    def comparison(self) -> Node:
        node = self.additive()
        t = self.peek()
        if t.kind == "op" and t.value in ("==", "!=", ">", ">=", "<", "<="):
            self.next()
            return Binary(op=t.value, left=node, right=self.additive(), pos=t.pos)
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.peek().kind == "op" and self.peek().value in ("+", "-"):
            t = self.next()
            node = Binary(op=t.value, left=node, right=self.multiplicative(), pos=t.pos)
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.peek().kind == "op" and self.peek().value in ("*", "/"):
            t = self.next()
            node = Binary(op=t.value, left=node, right=self.unary(), pos=t.pos)
        return node

    def unary(self) -> Node:
        t = self.peek()
        if t.kind == "op" and t.value == "-":
            self.next()
            return Unary(op="-", operand=self.unary(), pos=t.pos)
        return self.primary()

    def primary(self) -> Node:
        t = self.next()
        if t.kind == "num":
            return Lit(value=float(t.value), pos=t.pos)
        if t.kind == "str":
            return Lit(value=t.value, pos=t.pos)
        if t.kind == "field":
            return FieldRef(name=t.value, pos=t.pos)
        if t.kind == "lparen":
            node = self.or_()
            self.expect("rparen", "')'")
            return node
        if t.kind == "ident":
            low = t.value.lower()
            if low == "true":
                return Lit(value=True, pos=t.pos)
            if low == "false":
                return Lit(value=False, pos=t.pos)
            if low == "null":
                return Lit(value=None, pos=t.pos)
            if self.peek().kind == "lparen":
                if low not in _FUNCTIONS:
                    raise ExprError(
                        f"Unknown function '{t.value}'. Available: {', '.join(sorted(_FUNCTIONS))}",
                        t.pos,
                    )
                self.next()
                args: List[Node] = []
                if self.peek().kind != "rparen":
                    args.append(self.or_())
                    while self.peek().kind == "comma":
                        self.next()
                        args.append(self.or_())
                self.expect("rparen", "')'")
                lo, hi = _FUNCTIONS[low]
                if not (lo <= len(args) <= hi):
                    raise ExprError(
                        f"{low}() takes {lo}{'' if lo == hi else f'–{hi}'} arguments, got {len(args)}",
                        t.pos,
                    )
                return Func(name=low, args=args, pos=t.pos)
            return FieldRef(name=t.value, pos=t.pos)
        raise ExprError(f"Unexpected {t.value or 'end of expression'!r}", t.pos)

    def _kw(self, word: str) -> bool:
        t = self.peek()
        return t.kind == "ident" and t.value.lower() == word


def parse(src: str) -> Node:
    if len(src) > MAX_EXPR_LEN:
        raise ExprError(f"Expression exceeds {MAX_EXPR_LEN} characters")
    if not src.strip():
        raise ExprError("Expression is empty")
    return _Parser(_tokenize(src)).parse()


# ---------------------------------------------------------------------------
# Type inference (annotates node.type_; emitters rely on it)
# ---------------------------------------------------------------------------

_NUMERIC_FUNCS = {
    "abs": "quantitative",
    "round": "quantitative",
    "floor": "quantitative",
    "ceil": "quantitative",
}


def infer_type(node: Node, resolve: Callable[[str], Optional[str]]) -> str:
    """Annotate + return the node's result type; raises ExprError on misuse."""
    t = _infer(node, resolve)
    node.type_ = t
    return t


def _infer(node: Node, resolve: Callable[[str], Optional[str]]) -> str:
    if isinstance(node, Lit):
        if node.value is None:
            node.type_ = "null"
        elif isinstance(node.value, bool):
            node.type_ = "boolean"
        elif isinstance(node.value, float):
            node.type_ = "quantitative"
        else:
            node.type_ = "nominal"
        return node.type_
    if isinstance(node, FieldRef):
        t = resolve(node.name)
        if t is None:
            raise ExprError(f"Unknown field '{node.name}'", node.pos, unknown_field=node.name)
        node.type_ = "nominal" if t == "ordinal" else t
        return node.type_
    if isinstance(node, Unary):
        ot = _infer(node.operand, resolve)  # type: ignore[arg-type]
        if node.op == "-":
            _require(ot == "quantitative", node, "unary '-' needs a numeric operand")
            node.type_ = "quantitative"
        else:  # not
            _require(ot == "boolean", node, "'not' needs a boolean operand (a comparison)")
            node.type_ = "boolean"
        return node.type_
    if isinstance(node, Binary):
        lt = _infer(node.left, resolve)  # type: ignore[arg-type]
        rt = _infer(node.right, resolve)  # type: ignore[arg-type]
        if node.op in ("+", "-", "*", "/"):
            _require(
                lt == "quantitative" and rt == "quantitative",
                node,
                f"'{node.op}' needs numeric operands (got {lt} and {rt})",
            )
            node.type_ = "quantitative"
        elif node.op in ("and", "or"):
            _require(
                lt == "boolean" and rt == "boolean",
                node,
                f"'{node.op}' needs boolean operands (comparisons)",
            )
            node.type_ = "boolean"
        else:  # comparison
            _require(
                lt != "null" and rt != "null", node, "cannot compare against null — use coalesce()"
            )
            pair = {lt, rt}
            if pair == {"quantitative"} or pair == {"temporal"}:
                pass
            elif "temporal" in pair and pair - {"temporal"} == {"nominal"}:
                # temporal vs date-string literal — both sides coerce to timestamps
                pass
            elif pair == {"nominal"}:
                _require(
                    node.op in ("==", "!="),
                    node,
                    "text values only support '=' and '!=' comparisons",
                )
            elif pair == {"boolean"}:
                _require(node.op in ("==", "!="), node, "booleans only support '=' and '!='")
            else:
                _require(False, node, f"cannot compare {lt} with {rt}")
            node.type_ = "boolean"
        return node.type_
    if isinstance(node, Func):
        arg_types = [_infer(a, resolve) for a in node.args]
        if node.name in _NUMERIC_FUNCS:
            _require(
                arg_types[0] == "quantitative", node, f"{node.name}() needs a numeric argument"
            )
            if node.name == "round" and len(node.args) == 2:
                d = node.args[1]
                _require(
                    isinstance(d, Lit)
                    and isinstance(d.value, float)
                    and float(d.value).is_integer(),
                    node,
                    "round() digits must be an integer literal",
                )
            node.type_ = "quantitative"
        elif node.name in ("lower", "upper"):
            _require(arg_types[0] == "nominal", node, f"{node.name}() needs a text argument")
            node.type_ = "nominal"
        elif node.name == "length":
            _require(arg_types[0] == "nominal", node, "length() needs a text argument")
            node.type_ = "quantitative"
        elif node.name == "if":
            _require(arg_types[0] == "boolean", node, "if() condition must be a comparison")
            node.type_ = _unify(arg_types[1], arg_types[2], node)
        else:  # coalesce
            t = arg_types[0]
            for at in arg_types[1:]:
                t = _unify(t, at, node)
            node.type_ = t
        return node.type_
    raise ExprError("Invalid expression node", node.pos)


def _require(cond: bool, node: Node, message: str) -> None:
    if not cond:
        raise ExprError(message, node.pos)


def _unify(a: str, b: str, node: Node) -> str:
    if a == "null":
        return b
    if b == "null" or a == b:
        return a
    raise ExprError(f"branches must have the same type (got {a} and {b})", node.pos)


# ---------------------------------------------------------------------------
# Pandas evaluation
# ---------------------------------------------------------------------------


def _series(value: Any, index: pd.Index, dtype: Any = None) -> pd.Series:
    return pd.Series(value, index=index, dtype=dtype)


def _round_half_away(s: pd.Series, digits: int) -> pd.Series:
    """Postgres-style ROUND: halves away from zero (numpy rounds half to even)."""
    f = 10.0**digits
    return pd.Series(
        np.where(s.isna(), np.nan, np.copysign(np.floor(np.abs(s) * f + 0.5), s) / f),
        index=s.index,
    )


def _eval(node: Node, df: pd.DataFrame) -> pd.Series:
    idx = df.index
    if isinstance(node, Lit):
        if node.type_ == "boolean":
            return _series(node.value, idx, dtype="boolean")
        if node.type_ == "null":
            return _series(pd.NA, idx, dtype="object")
        return _series(node.value, idx)
    if isinstance(node, FieldRef):
        s = df[node.name]
        if node.type_ == "quantitative":
            return pd.to_numeric(s, errors="coerce")
        if node.type_ == "temporal":
            return pd.to_datetime(s, errors="coerce")
        return s.astype("string")
    if isinstance(node, Unary):
        v = _eval(node.operand, df)  # type: ignore[arg-type]
        return ~v if node.op == "not" else -v
    if isinstance(node, Binary):
        left = _eval(node.left, df)  # type: ignore[arg-type]
        right = _eval(node.right, df)  # type: ignore[arg-type]
        if node.op in ("and", "or"):
            # nullable-boolean dtype gives SQL's three-valued (Kleene) logic
            return (left & right) if node.op == "and" else (left | right)
        if node.op in ("+", "-", "*", "/"):
            if node.op == "+":
                return left + right
            if node.op == "-":
                return left - right
            if node.op == "*":
                return left * right
            return left / right.where(right != 0)  # x/0 → NaN, matches SQL NULLIF
        # comparisons — null-preserving (SQL: NULL <op> x → NULL)
        lt, rt = node.left.type_, node.right.type_  # type: ignore[union-attr]
        if "temporal" in (lt, rt):
            left = pd.to_datetime(left, errors="coerce")
            right = pd.to_datetime(right, errors="coerce")
        valid = left.notna() & right.notna()
        op = node.op
        if op == "==":
            raw = left == right
        elif op == "!=":
            raw = left != right
        elif op == ">":
            raw = left > right
        elif op == ">=":
            raw = left >= right
        elif op == "<":
            raw = left < right
        else:
            raw = left <= right
        return pd.Series(raw, index=df.index).astype("boolean").where(valid, other=pd.NA)
    if isinstance(node, Func):
        if node.name == "if":
            cond = _eval(node.args[0], df).fillna(False).astype(bool)
            a = _eval(node.args[1], df)
            b = _eval(node.args[2], df)
            return a.where(cond, b)
        if node.name == "coalesce":
            out = _eval(node.args[0], df)
            for arg in node.args[1:]:
                nxt = _eval(arg, df)
                out = out.where(out.notna(), nxt)
            return out
        arg = _eval(node.args[0], df)
        if node.name == "abs":
            return arg.abs()
        if node.name == "floor":
            return pd.Series(np.floor(arg.astype(float)), index=df.index)
        if node.name == "ceil":
            return pd.Series(np.ceil(arg.astype(float)), index=df.index)
        if node.name == "round":
            digits = int(node.args[1].value) if len(node.args) == 2 else 0  # type: ignore[union-attr]
            return _round_half_away(arg.astype(float), digits)
        if node.name == "lower":
            return arg.str.lower()
        if node.name == "upper":
            return arg.str.upper()
        # length — Float64 so nulls survive; output rounding normalizes later
        return arg.str.len().astype("Float64")
    raise ExprError("Invalid expression node", node.pos)


def materialize_pandas(node: Node, df: pd.DataFrame) -> pd.Series:
    """Evaluate to a column with the same value domain the SQL engine emits."""
    s = _eval(node, df)
    if node.type_ == "boolean":
        # Postgres boolean::text → 'true'/'false'; NULL stays null.
        return s.map({True: "true", False: "false"}).astype("string")
    if node.type_ == "quantitative":
        return pd.to_numeric(s, errors="coerce")
    if node.type_ == "temporal":
        return pd.to_datetime(s, errors="coerce")
    return s.astype("string")


# ---------------------------------------------------------------------------
# SQL emission (mirrors compile_sql conventions: quoted idents, '' escapes)
# ---------------------------------------------------------------------------


def _d():
    """Dialect of the statement being compiled (set by ``compile_spec_sql``)."""
    from datasources.sql_dialect import current_dialect  # noqa: PLC0415

    return current_dialect()


def _q(name: str) -> str:
    return _d().quote(name)


def _str_lit(v: str) -> str:
    if "\x00" in v:
        raise ExprError("String literal contains NUL byte")
    return _d().literal(v)


def _sql_field(node: FieldRef, catalog: FieldCatalog) -> str:
    d = _d()
    qc = _q(node.name)
    if node.type_ == "quantitative":
        info = catalog.get(node.name)
        from .catalog import PG_NUMERIC_TYPES  # noqa: PLC0415

        if info is not None and (info.db_type or "") in PG_NUMERIC_TYPES:
            return d.cast_float(qc)
        # text column the catalog calls numeric — regex-guarded cast like compile_sql
        return d.cast_float(d.numeric_guard(qc))
    if node.type_ == "temporal":
        return d.cast_timestamp(qc)
    return d.cast_text(qc)


def to_sql(node: Node, catalog: FieldCatalog) -> str:
    if isinstance(node, Lit):
        if node.value is None:
            return "NULL"
        if isinstance(node.value, bool):
            return _d().bool_literal(node.value)
        if isinstance(node.value, float):
            return str(int(node.value)) if node.value.is_integer() else repr(node.value)
        return _str_lit(str(node.value))
    if isinstance(node, FieldRef):
        return _sql_field(node, catalog)
    if isinstance(node, Unary):
        inner = to_sql(node.operand, catalog)  # type: ignore[arg-type]
        return f"(NOT {inner})" if node.op == "not" else f"(-{inner})"
    if isinstance(node, Binary):
        left = to_sql(node.left, catalog)  # type: ignore[arg-type]
        right = to_sql(node.right, catalog)  # type: ignore[arg-type]
        lt, rt = node.left.type_, node.right.type_  # type: ignore[union-attr]
        if node.op == "/":
            return f"({left} / NULLIF({right}, 0))"
        if node.op in ("+", "-", "*"):
            return f"({left} {node.op} {right})"
        if node.op in ("and", "or"):
            return f"({left} {node.op.upper()} {right})"
        op = {"==": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}[node.op]
        if "temporal" in (lt, rt):  # date-string literals coerce to timestamps
            if lt != "temporal":
                left = _d().cast_timestamp(left)
            if rt != "temporal":
                right = _d().cast_timestamp(right)
        return f"({left} {op} {right})"
    if isinstance(node, Func):
        args = [to_sql(a, catalog) for a in node.args]
        if node.name == "if":
            return f"(CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END)"
        if node.name == "coalesce":
            return f"COALESCE({', '.join(args)})"
        if node.name == "abs":
            return f"ABS({args[0]})"
        if node.name == "floor":
            return f"FLOOR({args[0]})"
        if node.name == "ceil":
            return _d().ceil(args[0])
        if node.name == "round":
            places = int(node.args[1].value) if len(node.args) == 2 else 0  # type: ignore[union-attr]
            return _d().round_float(args[0], places)
        if node.name == "lower":
            return f"LOWER({args[0]})"
        if node.name == "upper":
            return f"UPPER({args[0]})"
        return _d().cast_float(_d().length(args[0]))  # length
    raise ExprError("Invalid expression node", node.pos)


def materialize_sql(node: Node, catalog: FieldCatalog) -> str:
    """SQL expression casted to the same value domain the pandas engine emits."""
    d = _d()
    expr = to_sql(node, catalog)
    if node.type_ == "boolean":
        # A predicate is not a selectable value in every engine, so this is the
        # one cast that has to go through the dialect rather than a plain CAST.
        return d.bool_to_text(expr)
    if node.type_ == "quantitative":
        return d.cast_float(expr)
    if node.type_ == "temporal":
        return d.cast_timestamp(expr)
    return d.cast_text(expr)


# ---------------------------------------------------------------------------
# Resolution: spec calculations → typed, inlined, catalog-extendable columns
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCalc:
    name: str
    expr: str
    ast: Node
    field_type: str  # quantitative | nominal | temporal (boolean → nominal)


def _substitute(node: Node, known: Dict[str, Node]) -> Node:
    """Inline references to earlier calculated fields (so emitters only ever
    see real columns — no dependent projections needed in either engine)."""
    if isinstance(node, FieldRef) and node.name in known:
        return known[node.name]
    if isinstance(node, Unary):
        node.operand = _substitute(node.operand, known)  # type: ignore[arg-type]
    elif isinstance(node, Binary):
        node.left = _substitute(node.left, known)  # type: ignore[arg-type]
        node.right = _substitute(node.right, known)  # type: ignore[arg-type]
    elif isinstance(node, Func):
        node.args = [_substitute(a, known) for a in node.args]
    return node


def resolve_calculations(
    calcs: List[CalculateTransform],
    catalog: FieldCatalog,
    transform_indices: Optional[List[int]] = None,
) -> Tuple[List[ResolvedCalc], List[SpecError]]:
    """Parse + type every calculation; never raises (structured errors out)."""
    resolved: List[ResolvedCalc] = []
    errors: List[SpecError] = []
    known: Dict[str, Node] = {}

    def resolve_type(name: str) -> Optional[str]:
        info = catalog.get(name)
        return info.type if info is not None else None

    for i, c in enumerate(calcs):
        source_index = transform_indices[i] if transform_indices is not None else i
        path = f"transform[{source_index}]"
        name = (c.as_ or "").strip()
        if not name or len(name) > MAX_CALC_NAME_LEN:
            errors.append(
                SpecError(
                    path=f"{path}.as",
                    code="bad_calc_name",
                    message=f"Calculated field needs a name of 1–{MAX_CALC_NAME_LEN} characters.",
                )
            )
            continue
        if catalog.get(name) is not None or any(r.name == name for r in resolved):
            errors.append(
                SpecError(
                    path=f"{path}.as",
                    code="duplicate_field",
                    message=f"A field named '{name}' already exists.",
                )
            )
            continue
        try:
            ast = _substitute(parse(c.expr), known)
            infer_type(ast, resolve_type)
        except ExprError as exc:
            msg = exc.message
            if exc.unknown_field:
                names = catalog.field_names() + [r.name for r in resolved]
                close = difflib.get_close_matches(exc.unknown_field, names, n=2, cutoff=0.6)
                if close:
                    msg += f" Did you mean: {', '.join(close)}?"
            errors.append(SpecError(path=f"{path}.expr", code="expr_invalid", message=msg))
            continue
        if ast.type_ == "null":
            errors.append(
                SpecError(
                    path=f"{path}.expr",
                    code="expr_invalid",
                    message="Expression is always null.",
                )
            )
            continue
        field_type = "nominal" if ast.type_ == "boolean" else ast.type_
        resolved.append(ResolvedCalc(name=name, expr=c.expr, ast=ast, field_type=field_type))
        known[name] = ast
    return resolved, errors


def calc_field_info(calc: ResolvedCalc) -> FieldInfo:
    quantitative = calc.field_type == "quantitative"
    return FieldInfo(
        name=calc.name,
        type=calc.field_type,
        role="measure" if quantitative else "dimension",
        default_aggregate="sum" if quantitative else None,
        db_type=_CALC_DB_TYPE[calc.field_type],
        is_calculated=True,
    )


def extend_catalog(catalog: FieldCatalog, calcs: List[ResolvedCalc]) -> FieldCatalog:
    """Catalog + calculated columns; a copy, never mutates the cached catalog."""
    if not calcs:
        return catalog
    out = catalog.model_copy(deep=True)
    out.fields = list(out.fields) + [calc_field_info(c) for c in calcs]
    return out

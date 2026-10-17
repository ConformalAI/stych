"""
SQL-side diff for Postgres sources: compares the user's base table against the
``stych_temp`` working copy inside their own database, so full tables diff
without materializing either side locally.

Shape-compatible with ``diff_service.build_diff_page`` (the frontend cannot
tell the two engines apart):

- Row alignment by a detected unique key (base PK first, then uniqueness
  probes). No key → degraded diff: schema/row-count summary only, no
  cell-level annotations (positional alignment is meaningless in SQL — the
  physical order of an updated heap table is not stable).
- Summary in one FULL OUTER JOIN aggregate pass; per-cell old/new values only
  for the visible page.
- Transform classification (standardized/log/label-encoded/…) reuses the
  pandas detectors on a bounded aligned sample, then labels changed cells
  column-wise; NULL→value cells are classified "imputed" cell-wise.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from agents.sql_agent import working_copies as wc
from api.diff_detectors import (
    MISSING_TOKENS,
    classify_column,
    is_missing_value,
    normalize_missing,
)
from api.diff_service import LOW_SIMILARITY_THRESHOLD, _detect_renames, _equal_mask
from datasources.adapters import DIFF_TIMEOUT_MS, SAMPLE_ROWS, cached_count
from datasources.adapters.base import TableAdapter
from datasources.sql_dialect import get_dialect
from helpers.helper import to_json_safe

logger = logging.getLogger(__name__)

_diff_dialect: ContextVar[str] = ContextVar("sql_diff_dialect", default="postgres")

_KEY_CANDIDATE_LIMIT = 6
# Same float tolerance as diff_service._equal_mask (np.isclose defaults).
_FLOATISH_TYPES = {"real", "double precision", "float", "numeric", "decimal"}

# Column types that can only be "missing" via SQL NULL; anything else (text,
# varchar, char, uuid, json, …) can also hold a literal missing-token string.
_NON_TEXT_TYPES = {
    "integer",
    "bigint",
    "smallint",
    "numeric",
    "decimal",
    "real",
    "double precision",
    "float",
    "boolean",
    "bool",
    "date",
    "time",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
    "timestamptz",
    "smallserial",
    "serial",
    "bigserial",
}
# MISSING_TOKENS entries are simple literals with no embedded single quotes.
_SQL_MISSING_LITERALS = ", ".join("'" + t + "'" for t in sorted(MISSING_TOKENS))


def quote_ident(name: str) -> str:
    return _d().quote(name)


def _d():
    """Dialect for the diff being built."""
    return get_dialect(_diff_dialect.get())


def _as_text(expr: str) -> str:
    return get_dialect(_diff_dialect.get()).cast_text(expr)


def _bind_adapter_dialect(adapter: TableAdapter):
    return _diff_dialect.set(getattr(adapter, "dialect", None) or "postgres")


def missing_sql(expr: str, db_type: str) -> str:
    """SQL boolean mirroring ``diff_detectors.is_missing_value``: is ``expr``
    semantically missing (NULL, or a missing-token string on a text column)?"""
    if (db_type or "").lower() in _NON_TEXT_TYPES:
        return f"{expr} IS NULL"
    trimmed = get_dialect(_diff_dialect.get()).trim_lower(expr)
    return f"({expr} IS NULL OR {trimmed} IN ({_SQL_MISSING_LITERALS}))"


_SPEC_CACHE_MAX = 2
_spec_cache: "OrderedDict[Tuple, _DiffSpec]" = OrderedDict()


@dataclass
class _DisplayCol:
    name: str
    dtype: str
    kind: str
    base_col: Optional[str] = None  # None => column only exists in the target
    base_type: str = ""
    target_type: str = ""
    pair_index: Optional[int] = None  # index into the _chg/_old select aliases


@dataclass
class _DiffSpec:
    key: Optional[str]
    join_on: str = ""
    display: List[_DisplayCol] = field(default_factory=list)
    columns_spec: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    # Per compared column: dominant kind/label for non-imputed changed cells,
    # plus the label to put on NULL→value (imputed) cells.
    col_class: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)


# ------------------------------------------------------------- SQL fragments


def changed_predicate(b_expr: str, t_expr: str, b_type: str, t_type: str) -> str:
    """NULL-safe per-column "changed" predicate. Mismatched types (a recast
    happened in the working copy) compare as text — comparing them raw would
    error the whole query."""
    if (b_type or "").lower() != (t_type or "").lower():
        return _d().distinct_from(_as_text(b_expr), _as_text(t_expr))
    if (b_type or "").lower() in _FLOATISH_TYPES:
        tol = (
            f"abs({b_expr} - {t_expr}) <= "
            f"{_d().greatest(f'abs({b_expr})', f'abs({t_expr})')} * 1e-9 + 1e-12"
        )
        return (
            f"({_d().distinct_from(b_expr, t_expr)} AND NOT "
            f"({b_expr} IS NOT NULL AND {t_expr} IS NOT NULL AND {tol}))"
        )
    return _d().distinct_from(b_expr, t_expr)


def _display_expr(col: _DisplayCol, key_quoted: str) -> str:
    """Value shown in the grid: the target's, except on ghost rows where only
    the base has the record."""
    if col.base_col is None:
        return f"t.{quote_ident(col.name)}"
    b = f"b.{quote_ident(col.base_col)}"
    t = f"t.{quote_ident(col.name)}"
    if (col.base_type or "").lower() != (col.target_type or "").lower():
        b, t = _as_text(b), _as_text(t)
    return f"CASE WHEN t.{key_quoted} IS NULL THEN {b} ELSE {t} END"


def build_join_sql(spec: _DiffSpec, base_ref: str, temp_ref: str) -> str:
    """The inner SELECT: display values plus row flags and per-column change
    flags/old values, over the FULL OUTER JOIN. Wrapped as a subquery so WHERE
    and ORDER BY can reference the aliases."""
    kq = quote_ident(spec.key)
    items = [f"{_display_expr(c, kq)} AS d{j}" for j, c in enumerate(spec.display)]
    items.append(f"{_d().select_bool(f'b.{kq} IS NULL')} AS _added")
    items.append(f"{_d().select_bool(f't.{kq} IS NULL')} AS _ghost")
    matched = f"(b.{kq} IS NOT NULL AND t.{kq} IS NOT NULL)"
    for c in spec.display:
        if c.pair_index is None:
            continue
        i = c.pair_index
        pred = changed_predicate(
            f"b.{quote_ident(c.base_col)}",
            f"t.{quote_ident(c.name)}",
            c.base_type,
            c.target_type,
        )
        items.append(f"{_d().select_bool(f'{matched} AND {pred}')} AS _chg{i}")
        items.append(f"b.{quote_ident(c.base_col)} AS _old{i}")
    items.append(f"COALESCE({_as_text(f't.{kq}')}, {_as_text(f'b.{kq}')}) AS _ord")
    return (
        f"SELECT {', '.join(items)} FROM {base_ref} b "
        f"FULL OUTER JOIN {temp_ref} t ON {spec.join_on}"
    )


def build_summary_sql(spec: _DiffSpec, base_ref: str, temp_ref: str) -> str:
    kq = quote_ident(spec.key)
    matched = f"(b.{kq} IS NOT NULL AND t.{kq} IS NOT NULL)"
    preds = []
    for c in spec.display:
        if c.pair_index is None:
            continue
        preds.append(
            (
                c.pair_index,
                changed_predicate(
                    f"b.{quote_ident(c.base_col)}",
                    f"t.{quote_ident(c.name)}",
                    c.base_type,
                    c.target_type,
                ),
            )
        )
    any_chg = " OR ".join(p for _, p in preds) if preds else _d().bool_literal(False)
    items = [
        f"{_d().count_if(f'b.{kq} IS NULL')} AS rows_added",
        f"{_d().count_if(f't.{kq} IS NULL')} AS rows_removed",
        f"{_d().count_if(f'{matched} AND ({any_chg})')} AS rows_changed",
        f"{_d().count_if(f'{matched} AND NOT ({any_chg})')} AS rows_unchanged",
    ]
    for i, pred in preds:
        items.append(f"{_d().count_if(f'{matched} AND {pred}')} AS c{i}")
    return (
        f"SELECT {', '.join(items)} FROM {base_ref} b "
        f"FULL OUTER JOIN {temp_ref} t ON {spec.join_on}"
    )


def build_mode_where(
    spec: _DiffSpec,
    *,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """WHERE over the join subquery ``q``: changes_only / change-kind chips and
    the grid's column filters + global search (over the display expressions)."""
    from datasources.adapters.base import escape_like  # noqa: PLC0415

    pairs = [c for c in spec.display if c.pair_index is not None]
    chg_flags = [_d().test_bool(f"q._chg{c.pair_index}") for c in pairs]
    clauses: List[str] = []
    params: Dict[str, str] = {}

    if changes_only:
        any_chg = " OR ".join(chg_flags) if chg_flags else _d().bool_literal(False)
        clauses.append(
            f"({_d().test_bool('q._added')} OR {_d().test_bool('q._ghost')} OR {any_chg})"
        )

    if change_kind == "added":
        clauses.append(_d().test_bool("q._added"))
    elif change_kind == "removed":
        clauses.append(_d().test_bool("q._ghost"))
    elif change_kind == "imputed":
        ors = [
            f"({_d().test_bool(f'q._chg{c.pair_index}')} AND "
            f"{missing_sql(f'q._old{c.pair_index}', c.base_type)})"
            for c in pairs
        ]
        clauses.append("(" + (" OR ".join(ors) if ors else "FALSE") + ")")
    elif change_kind in ("changed", "transformed"):
        ors = [
            f"({_d().test_bool(f'q._chg{c.pair_index}')} AND NOT "
            f"{missing_sql(f'q._old{c.pair_index}', c.base_type)})"
            for c in pairs
            if (spec.col_class.get(c.name, {}).get("kind") or "changed") == change_kind
        ]
        clauses.append("(" + (" OR ".join(ors) if ors else "FALSE") + ")")

    names = {c.name: j for j, c in enumerate(spec.display)}
    for i, (name, needle) in enumerate((filters or {}).items()):
        j = names.get(name)
        if j is None or not needle:
            continue
        params[f"f{i}"] = f"%{escape_like(needle)}%"
        clauses.append(_d().ilike(_as_text(f"q.d{j}"), f":f{i}"))
    if search and spec.display:
        params["srch"] = f"%{escape_like(search)}%"
        ors = " OR ".join(
            _d().ilike(_as_text(f"q.d{j}"), ":srch") for j in range(len(spec.display))
        )
        clauses.append(f"({ors})")

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def build_page_sql(
    spec: _DiffSpec,
    base_ref: str,
    temp_ref: str,
    *,
    offset: int,
    limit: int,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    inner = build_join_sql(spec, base_ref, temp_ref)
    where_sql, params = build_mode_where(
        spec,
        changes_only=changes_only,
        change_kind=change_kind,
        filters=filters,
        search=search,
    )
    order_parts = ["q._ghost ASC"]  # ghost (removed) rows sink below target rows
    names = {c.name: j for j, c in enumerate(spec.display)}
    if sort_by and sort_by in names:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_parts.append(_d().order_term(f"q.d{names[sort_by]}", direction.lower()))
    order_parts.append("q._ord")
    params.update({"limit": int(limit), "offset": int(offset)})
    sql = (
        f"SELECT q.* FROM ({inner}) q{where_sql} ORDER BY {', '.join(order_parts)}"
        f"{_d().pagination_suffix(':limit', ':offset')}"
    )
    return sql, params


def build_filtered_count_sql(
    spec: _DiffSpec,
    base_ref: str,
    temp_ref: str,
    *,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    inner = build_join_sql(spec, base_ref, temp_ref)
    where_sql, params = build_mode_where(
        spec,
        changes_only=changes_only,
        change_kind=change_kind,
        filters=filters,
        search=search,
    )
    return f"SELECT COUNT(*) FROM ({inner}) q{where_sql}", params


# ------------------------------------------------------------- key detection


def detect_key(base: TableAdapter, temp: TableAdapter, common: List[str]) -> Optional[str]:
    """Single-column primary key of the base table when it survives in the
    working copy; else the first common column that probes unique+non-null on
    both sides (id-like names first)."""
    common_set = set(common)
    if base.is_table:
        try:
            pk_query = _d().primary_key_query(base.db_schema, base.table)
            if pk_query is None:
                raise LookupError("Dialect has no trusted primary-key catalog query")
            sql, params = pk_query
            _, rows = base.run(sql, params)
            pk_cols = [r[0] for r in rows]
            if len(pk_cols) == 1 and pk_cols[0] in common_set:
                if _probe_unique(temp, pk_cols[0]):
                    return pk_cols[0]
        except Exception as exc:  # noqa: BLE001 — PK lookup is best-effort
            logger.debug("[sql-diff] PK lookup failed: %s", exc)

    def rank(name: str) -> int:
        n = name.lower()
        if n == "id":
            return 0
        if n.endswith("_id"):
            return 1
        if n in ("index", "key"):
            return 2
        return 3

    candidates = sorted(common, key=lambda c: (rank(c), common.index(c)))
    for col in candidates[:_KEY_CANDIDATE_LIMIT]:
        try:
            if _probe_unique(base, col) and _probe_unique(temp, col):
                return col
        except Exception as exc:  # noqa: BLE001 — a probe failure just skips
            logger.debug("[sql-diff] key probe failed for %s: %s", col, exc)
    return None


def _probe_unique(adapter: TableAdapter, col: str) -> bool:
    q = quote_ident(col)
    _, rows = adapter.run(
        f"SELECT COUNT(*), COUNT(DISTINCT {q}), COUNT({q}) FROM {adapter.source_ref}",
        timeout_ms=DIFF_TIMEOUT_MS,
    )
    return bool(rows and rows[0][0] == rows[0][1] == rows[0][2])


# ---------------------------------------------------------------- spec build


def _coerce_numeric(series: pd.Series) -> pd.Series:
    if series.dtype != object:
        return series
    coerced = pd.to_numeric(series, errors="coerce")
    # Only adopt the numeric view when it didn't destroy real values.
    if coerced.notna().sum() >= series.notna().sum():
        return coerced
    return series


def _pull_sample(
    base: TableAdapter,
    temp: TableAdapter,
    key: str,
    join_on: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Aligned (base_sample, target_sample) frames of matched rows, positional
    aliases so odd column names can't collide."""
    base_cols = [c.name for c in base.schema()]
    temp_cols = [c.name for c in temp.schema()]
    items = [f"b.{quote_ident(c)} AS b__{i}" for i, c in enumerate(base_cols)]
    items += [f"t.{quote_ident(c)} AS t__{i}" for i, c in enumerate(temp_cols)]
    sql = (
        f"SELECT {_d().select_prefix(SAMPLE_ROWS)}{', '.join(items)} "
        f"FROM {base.source_ref} b JOIN {temp.source_ref} t ON {join_on} "
        f"ORDER BY b.{quote_ident(key)}{_d().limit_suffix(SAMPLE_ROWS)}"
    )
    if hasattr(base, "run_df"):
        frame = base.run_df(sql, timeout_ms=DIFF_TIMEOUT_MS)
    else:
        keys, rows = base.run(sql, timeout_ms=DIFF_TIMEOUT_MS)
        frame = pd.DataFrame.from_records(rows, columns=keys)
    # Snowflake uppercases unquoted result aliases (``B__0``/``T__0``), while
    # Postgres preserves the lower-case aliases generated above.  Normalize
    # this bounded, alias-only frame so diff classification is dialect-neutral.
    frame.columns = [str(column).lower() for column in frame.columns]
    base_sample = pd.DataFrame(
        {c: _coerce_numeric(frame[f"b__{i}"]) for i, c in enumerate(base_cols)}
    )
    target_sample = pd.DataFrame(
        {c: _coerce_numeric(frame[f"t__{i}"]) for i, c in enumerate(temp_cols)}
    )
    return base_sample, target_sample


def _classify_from_sample(
    base_sample: pd.DataFrame, target_sample: pd.DataFrame, name: str, base_col: str
) -> Tuple[Dict[str, Optional[str]], Optional[str]]:
    """Column-level classification on the sample: dominant kind/label for
    value→value changes, label for NULL→value (imputed) cells, column note."""
    # Match the render side: literal missing-tokens ("" / "NA" / "UNKNOWN" / …)
    # count as missing, so a fill over them is labeled as imputation, not a
    # value->value change or a "remap".
    bv = normalize_missing(base_sample[base_col].reset_index(drop=True))
    tv = normalize_missing(target_sample[name].reset_index(drop=True))
    changed = ~_equal_mask(bv, tv)
    if not changed.any():
        return {"kind": "changed", "label": None, "imputed_label": None}, None
    kinds, labels, note = classify_column(bv, tv, changed)
    labels = labels.where(labels.notna(), None)
    value_changed = changed & bv.notna()
    imputed = changed & bv.isna()

    def _mode(series: pd.Series) -> Optional[str]:
        vals = series.dropna()
        return str(vals.mode().iloc[0]) if len(vals) else None

    return {
        "kind": _mode(kinds[value_changed]) or "changed",
        "label": _mode(labels[value_changed].astype(object)),
        "imputed_label": _mode(labels[imputed].astype(object)),
    }, note


def _build_spec(base: TableAdapter, temp: TableAdapter) -> _DiffSpec:
    base_cols = {c.name: c for c in base.schema()}
    temp_schema = temp.schema()
    common = [c.name for c in temp_schema if c.name in base_cols]
    # The stamped row id is Stych's, not the user's: it may align rows but must
    # never surface as a column, least of all as a spurious "added column".
    added = [
        c.name for c in temp_schema if c.name not in base_cols and not wc.is_internal_column(c.name)
    ]
    removed = [
        n
        for n in base_cols
        if n not in {c.name for c in temp_schema} and not wc.is_internal_column(n)
    ]

    key = detect_key(base, temp, common)
    if key is None:
        return _degraded_spec(base, temp, temp_schema, common, added, removed)

    kq = quote_ident(key)
    b_key_type = (base_cols[key].db_type or "").lower()
    t_key_type = next((c.db_type or "").lower() for c in temp_schema if c.name == key)
    join_on = (
        f"b.{kq} = t.{kq}"
        if b_key_type == t_key_type
        else f"{_as_text(f'b.{kq}')} = {_as_text(f't.{kq}')}"
    )

    base_sample, target_sample = _pull_sample(base, temp, key, join_on)
    renames = _detect_renames(
        base_sample, target_sample, removed, added, np.arange(len(base_sample))
    )
    added = [c for c in added if c not in renames]
    removed = [c for c in removed if c not in set(renames.values())]

    temp_types = {c.name: c.db_type for c in temp_schema}
    display: List[_DisplayCol] = []
    columns_spec: List[Dict[str, Any]] = []
    col_class: Dict[str, Dict[str, Optional[str]]] = {}
    pair_index = 0
    for c in temp_schema:
        if wc.is_internal_column(c.name):
            continue
        base_col = c.name if c.name in base_cols else renames.get(c.name)
        dcol = _DisplayCol(
            name=c.name,
            dtype=c.db_type,
            kind=c.kind,
            base_col=base_col,
            base_type=base_cols[base_col].db_type if base_col else "",
            target_type=temp_types[c.name],
        )
        spec_entry: Dict[str, Any] = {
            "name": c.name,
            "dtype": c.db_type,
            "kind": c.kind,
            "status": "common",
            "note": None,
            "changed_cells": 0,
        }
        if base_col is None:
            spec_entry["status"] = "added"
            spec_entry["note"] = "new column"
        else:
            if c.name in renames:
                spec_entry["status"] = "renamed"
                spec_entry["note"] = f"renamed from {renames[c.name]}"
            dcol.pair_index = pair_index
            pair_index += 1
            klass, note = _classify_from_sample(base_sample, target_sample, c.name, base_col)
            col_class[c.name] = klass
            if note and spec_entry["note"] is None:
                spec_entry["note"] = note
        display.append(dcol)
        columns_spec.append(spec_entry)

    spec = _DiffSpec(
        key=key,
        join_on=join_on,
        display=display,
        columns_spec=columns_spec,
        col_class=col_class,
    )

    _, rows = base.run(
        build_summary_sql(spec, base.source_ref, temp.source_ref), timeout_ms=DIFF_TIMEOUT_MS
    )
    keys = ["rows_added", "rows_removed", "rows_changed", "rows_unchanged"]
    agg = dict(zip(keys, rows[0][: len(keys)]))
    per_col = rows[0][len(keys) :]
    cells_changed = 0
    for c, spec_entry in zip(display, columns_spec):
        if c.pair_index is None:
            continue
        n = int(per_col[c.pair_index] or 0)
        spec_entry["changed_cells"] = n
        cells_changed += n

    n_base_cols = max(len(base_cols), 1)
    n_temp_cols = max(len(temp_schema), 1)
    overlap = len(common) + len(renames)
    similarity = overlap / max(n_base_cols, n_temp_cols)
    warning = None
    if overlap == 0:
        warning = (
            "These two sources share no columns — they do not appear to be "
            "versions of the same dataset."
        )
    elif similarity < LOW_SIMILARITY_THRESHOLD:
        warning = (
            f"Low overlap: only {overlap} of {max(n_base_cols, n_temp_cols)} "
            "columns are shared. The comparison may not be meaningful."
        )

    spec.summary = {
        "rows_added": int(agg["rows_added"] or 0),
        "rows_removed": int(agg["rows_removed"] or 0),
        "rows_changed": int(agg["rows_changed"] or 0),
        "rows_unchanged": int(agg["rows_unchanged"] or 0),
        "cells_changed": cells_changed,
        "columns_added": [str(c) for c in added],
        "columns_removed": [str(c) for c in removed],
        "columns_renamed": {str(new): str(old) for new, old in renames.items()},
        "similarity": round(similarity, 3),
        "alignment": f"key:{key}",
        "warning": warning,
    }
    return spec


def _degraded_spec(
    base: TableAdapter,
    temp: TableAdapter,
    temp_schema,
    common: List[str],
    added: List[str],
    removed: List[str],
) -> _DiffSpec:
    base_total = base.count().value
    temp_total = temp.count().value
    n_cols = max(len(base.schema()), len(temp_schema), 1)
    similarity = len(common) / n_cols
    spec = _DiffSpec(
        key=None,
        display=[_DisplayCol(name=c.name, dtype=c.db_type, kind=c.kind) for c in temp_schema],
        columns_spec=[
            {
                "name": c.name,
                "dtype": c.db_type,
                "kind": c.kind,
                "status": "common" if c.name in set(common) else "added",
                "note": None if c.name in set(common) else "new column",
                "changed_cells": 0,
            }
            for c in temp_schema
        ],
    )
    spec.summary = {
        "rows_added": max(0, temp_total - base_total),
        "rows_removed": max(0, base_total - temp_total),
        "rows_changed": 0,
        "rows_unchanged": min(base_total, temp_total),
        "cells_changed": 0,
        "columns_added": [str(c) for c in added],
        "columns_removed": [str(c) for c in removed],
        "columns_renamed": {},
        "similarity": round(similarity, 3),
        "alignment": "none",
        "warning": (
            "No unique key column found — showing the working table without cell-level changes."
        ),
    }
    return spec


def get_cached_spec(cache_key: Tuple, base: TableAdapter, temp: TableAdapter) -> _DiffSpec:
    hit = _spec_cache.get(cache_key)
    if hit is not None:
        _spec_cache.move_to_end(cache_key)
        return hit
    spec = _build_spec(base, temp)
    _spec_cache[cache_key] = spec
    while len(_spec_cache) > _SPEC_CACHE_MAX:
        _spec_cache.popitem(last=False)
    return spec


def clear_spec_cache() -> None:
    """Test helper."""
    _spec_cache.clear()


# ----------------------------------------------------------------- page build


def build_sql_diff_page(
    base: TableAdapter,
    temp: TableAdapter,
    *,
    session_id: str,
    page: int = 1,
    page_size: int = 100,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
) -> Dict[str, Any]:
    dialect_token = _bind_adapter_dialect(base)
    try:
        return _build_sql_diff_page_impl(
            base,
            temp,
            session_id=session_id,
            page=page,
            page_size=page_size,
            changes_only=changes_only,
            change_kind=change_kind,
            filters=filters,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    finally:
        _diff_dialect.reset(dialect_token)


def _build_sql_diff_page_impl(
    base: TableAdapter,
    temp: TableAdapter,
    *,
    session_id: str,
    page: int = 1,
    page_size: int = 100,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
) -> Dict[str, Any]:
    cache_key = (session_id, *base.cache_info.key, *temp.cache_info.key)
    spec = get_cached_spec(cache_key, base, temp)

    if spec.key is None:
        return _degraded_page(
            spec,
            temp,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filters=filters,
            search=search,
        )

    summary = spec.summary
    total_rows = (
        summary["rows_changed"]
        + summary["rows_unchanged"]
        + summary["rows_added"]
        + summary["rows_removed"]
    )
    has_narrowing = bool(changes_only or change_kind or filters or search)
    if has_narrowing:
        filters_key = json.dumps(filters or {}, sort_keys=True)
        count_sql, count_params = build_filtered_count_sql(
            spec,
            base.source_ref,
            temp.source_ref,
            changes_only=changes_only,
            change_kind=change_kind,
            filters=filters,
            search=search,
        )
        filtered = cached_count(
            (session_id, *cache_key, "diff", changes_only, change_kind, filters_key, search),
            temp.cache_info.ttl,
            lambda: _run_count(base, count_sql, count_params),
        ).value
    else:
        filtered = total_rows

    total_pages = max(1, ceil(filtered / page_size))
    page = min(max(1, page), total_pages)
    sql, params = build_page_sql(
        spec,
        base.source_ref,
        temp.source_ref,
        offset=(page - 1) * page_size,
        limit=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
        changes_only=changes_only,
        change_kind=change_kind,
        filters=filters,
        search=search,
    )
    keys, raw_rows = base.run(sql, params, timeout_ms=DIFF_TIMEOUT_MS)
    # Snowflake uppercases unquoted result aliases (D0/_CHG0/…), while
    # Postgres preserves the lower-case aliases used by the result shaper.
    index = {str(key).lower(): i for i, key in enumerate(keys)}

    n_display = len(spec.display)
    rows: List[List[Any]] = []
    cell_meta: List[List[Optional[Dict[str, Any]]]] = []
    for raw in raw_rows:
        rows.append([to_json_safe(raw[index[f"d{j}"]]) for j in range(n_display)])
        cell_meta.append(_row_meta(spec, raw, index))

    return {
        "kind": "diff",
        "columns": spec.columns_spec,
        "rows": rows,
        "cell_meta": cell_meta,
        "total_rows": total_rows,
        "filtered_rows": filtered,
        "page": page,
        "total_pages": total_pages,
        "summary": summary,
    }


def _run_count(base: TableAdapter, sql: str, params: Dict[str, Any]):
    from datasources.adapters import CountResult  # noqa: PLC0415

    _, rows = base.run(sql, params, timeout_ms=DIFF_TIMEOUT_MS)
    return CountResult(int(rows[0][0]))


def _row_meta(spec: _DiffSpec, raw: tuple, index: Dict[str, int]) -> List[Optional[Dict[str, Any]]]:
    if raw[index["_ghost"]]:
        # No "o": the frontend reads its absence on a removed cell as
        # "this whole row is not present in the target".
        return [{"k": "removed"} for _ in spec.display]
    if raw[index["_added"]]:
        return [{"k": "added"} for _ in spec.display]
    meta: List[Optional[Dict[str, Any]]] = []
    for c in spec.display:
        if c.pair_index is None:
            # Wholly-new column: every cell is an addition (pandas parity).
            meta.append({"k": "added"})
            continue
        if not raw[index[f"_chg{c.pair_index}"]]:
            meta.append(None)
            continue
        old = raw[index[f"_old{c.pair_index}"]]
        klass = spec.col_class.get(c.name, {})
        # A base cell that is NULL *or* a literal missing-token was "missing";
        # filling it is an imputation, not a value->value change.
        if is_missing_value(old):
            cell: Dict[str, Any] = {"k": "imputed"}
            if klass.get("imputed_label"):
                cell["l"] = klass["imputed_label"]
            cell["o"] = None
        else:
            cell = {"k": klass.get("kind") or "changed"}
            if klass.get("label"):
                cell["l"] = klass["label"]
            cell["o"] = to_json_safe(old)
        meta.append(cell)
    return meta


def _degraded_page(
    spec: _DiffSpec,
    temp: TableAdapter,
    *,
    page: int,
    page_size: int,
    sort_by: Optional[str],
    sort_dir: str,
    filters: Optional[Dict[str, str]],
    search: Optional[str],
) -> Dict[str, Any]:
    """No key → serve plain working-table rows (still pushed down) with the
    schema-level summary; change-kind narrowing has nothing to bind to."""
    total = temp.count().value
    filtered = temp.count(filters, search).value if (filters or search) else total
    total_pages = max(1, ceil(filtered / page_size))
    page = min(max(1, page), total_pages)
    raw_rows = temp.fetch_page(
        offset=(page - 1) * page_size,
        limit=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
        filters=filters,
        search=search,
    )
    rows = [[to_json_safe(v) for v in row] for row in raw_rows]
    return {
        "kind": "diff",
        "columns": spec.columns_spec,
        "rows": rows,
        "cell_meta": [[None] * len(spec.display) for _ in rows],
        "total_rows": total,
        "filtered_rows": filtered,
        "page": page,
        "total_pages": total_pages,
        "summary": spec.summary,
    }

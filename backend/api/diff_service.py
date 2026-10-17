"""
Spreadsheet diff engine: aligns two tabular sources (base -> target), classifies
every changed cell (imputation, scaling, encoding, recasts, ...), and serves
paginated pages of the target frame annotated with those classifications.

Alignment is by a detected key column when possible, else by row position.
Classification is two-tier: column-level transform detectors run first
(standardized, min-max, log, constant scale/shift, label encoding, recast),
then per-cell detectors (imputation method, rounding, case/trim, remapping),
with a generic "changed" fallback.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from api.diff_detectors import classify_column, normalize_missing
from api.preview_service import _contains_mask, column_kind
from helpers.helper import to_json_safe

RENAME_MATCH_RATIO = 0.98
MAX_RENAME_CANDIDATES = 12
MIN_RENAME_ROWS = 3
LOW_SIMILARITY_THRESHOLD = 0.5

_CACHE_MAX_ENTRIES = 2
_diff_cache: "OrderedDict[Tuple, DiffResult]" = OrderedDict()


@dataclass
class DiffResult:
    columns: List[Dict[str, Any]]
    values: pd.DataFrame  # target rows then ghost (removed base) rows
    kinds: np.ndarray  # (rows, cols) object: diff kind or None
    labels: np.ndarray  # (rows, cols) object: short label or None
    olds: np.ndarray  # (rows, cols) object: previous value where changed
    changed_rows: np.ndarray  # bool per display row
    # True for appended ghost rows (removed base records).
    ghost_rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    # Per-kind row masks driving the summary-chip / legend filters.
    row_kind_masks: Dict[str, np.ndarray] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ alignment


def _detect_key(base: pd.DataFrame, target: pd.DataFrame, common: List) -> Optional[Any]:
    """A usable join key: unique + non-null in both frames; prefer id-like names."""
    candidates = []
    for col in common:
        b, t = base[col], target[col]
        if b.isna().any() or t.isna().any():
            continue
        try:
            if b.nunique() == len(b) and t.nunique() == len(t):
                candidates.append(col)
        except TypeError:  # unhashable cells
            continue
    if not candidates:
        return None
    for col in candidates:
        name = str(col).lower()
        if name == "id" or name.endswith("_id") or name in ("index", "key"):
            return col
    return candidates[0]


def _align_rows(
    base: pd.DataFrame, target: pd.DataFrame, key: Optional[Any]
) -> Tuple[np.ndarray, np.ndarray, int, str]:
    """Returns (base position per target row [-1 = no match], added-row mask,
    removed-row count, alignment description)."""
    if key is not None:
        base_pos = pd.Series(np.arange(len(base)), index=base[key])
        matched = target[key].map(base_pos)
        row_base_pos = matched.fillna(-1).astype(int).to_numpy()
        added = row_base_pos < 0
        removed = len(base) - int((row_base_pos >= 0).sum())
        return row_base_pos, added, removed, f"key:{key}"

    n, m = len(target), len(base)
    row_base_pos = np.arange(n)
    row_base_pos[m:] = -1
    added = row_base_pos < 0
    removed = max(0, m - n)
    return row_base_pos, added, removed, "positional"


def _equal_mask(bv: pd.Series, tv: pd.Series) -> pd.Series:
    """Elementwise equality treating NaN==NaN as equal and floats with tolerance."""
    both_na = bv.isna() & tv.isna()
    if pd.api.types.is_numeric_dtype(bv) and pd.api.types.is_numeric_dtype(tv):
        b = pd.to_numeric(bv, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        t = pd.to_numeric(tv, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        eq = pd.Series(np.isclose(b, t, rtol=1e-09, atol=1e-12), index=tv.index)
    else:
        eq = (bv == tv).fillna(False)
        eq = eq.astype(bool)
    return eq | both_na


def _detect_renames(
    base: pd.DataFrame,
    target: pd.DataFrame,
    removed_cols: List,
    added_cols: List,
    row_base_pos: np.ndarray,
) -> Dict[Any, Any]:
    """Map added target column -> removed base column when values match.

    Only unambiguous 1:1 matches count: if a column's values match several
    candidates (duplicate-valued columns), claiming a rename would be a guess.
    """
    matched_rows = row_base_pos >= 0
    if matched_rows.sum() < MIN_RENAME_ROWS:
        return {}
    matches: List[Tuple[Any, Any]] = []
    for ac in added_cols[:MAX_RENAME_CANDIDATES]:
        tv = target[ac].reset_index(drop=True)[matched_rows]
        for rc in removed_cols[:MAX_RENAME_CANDIDATES]:
            bv = base[rc].iloc[np.clip(row_base_pos, 0, None)].reset_index(drop=True)[matched_rows]
            try:
                ratio = _equal_mask(bv, tv).mean()
            except (TypeError, ValueError):
                continue
            if ratio >= RENAME_MATCH_RATIO:
                matches.append((ac, rc))
    ac_counts = pd.Series([m[0] for m in matches]).value_counts() if matches else {}
    rc_counts = pd.Series([m[1] for m in matches]).value_counts() if matches else {}
    return {ac: rc for ac, rc in matches if ac_counts[ac] == 1 and rc_counts[rc] == 1}


# ------------------------------------------------------------------- compute


def compute_diff(base: pd.DataFrame, target: pd.DataFrame) -> DiffResult:
    common = [c for c in base.columns if c in set(target.columns)]
    added_cols = [c for c in target.columns if c not in set(base.columns)]
    removed_cols = [c for c in base.columns if c not in set(target.columns)]

    key = _detect_key(base, target, common)
    row_base_pos, rows_added, rows_removed, alignment = _align_rows(base, target, key)
    renames = _detect_renames(base, target, removed_cols, added_cols, row_base_pos)
    added_cols = [c for c in added_cols if c not in renames]
    removed_cols = [c for c in removed_cols if c not in renames.values()]

    # One-hot detection: a removed categorical column whose values reappear as
    # `col_<value>`-named added indicator columns.
    onehot_sources = {}
    for rc in list(removed_cols):
        prefix = f"{rc}_"
        members = [c for c in added_cols if str(c).startswith(prefix)]
        if len(members) >= 2 and all(
            set(pd.unique(target[m].dropna())) <= {0, 1, 0.0, 1.0, True, False} for m in members
        ):
            for m in members:
                onehot_sources[m] = rc

    n_rows = len(target)
    display_cols = list(target.columns)
    shape = (n_rows, len(display_cols))
    kinds = np.full(shape, None, dtype=object)
    labels = np.full(shape, None, dtype=object)
    olds = np.full(shape, None, dtype=object)

    matched = row_base_pos >= 0
    base_aligned = base.iloc[np.clip(row_base_pos, 0, None)].reset_index(drop=True)

    columns_spec: List[Dict[str, Any]] = []
    cells_changed = 0

    for j, col in enumerate(display_cols):
        tv = target[col].reset_index(drop=True)
        spec: Dict[str, Any] = {
            "name": str(col),
            "dtype": str(target[col].dtype),
            "kind": column_kind(target[col].dtype),
            "status": "common",
            "note": None,
            "changed_cells": 0,
        }

        if col in renames:
            spec["status"] = "renamed"
            spec["note"] = f"renamed from {renames[col]}"
        elif col in onehot_sources:
            spec["status"] = "added"
            spec["note"] = f"one-hot of {onehot_sources[col]}"
            kinds[:, j] = "added"
            columns_spec.append(spec)
            continue
        elif col in added_cols:
            spec["status"] = "added"
            spec["note"] = "new column"
            kinds[:, j] = "added"
            columns_spec.append(spec)
            continue

        bcol = renames.get(col, col)
        # Treat literal missing-tokens (e.g. "" / "NA" / "UNKNOWN") as NaN so a
        # missing->value fill is classified "imputed", matching the SQL engine.
        bv = normalize_missing(base_aligned[bcol].reset_index(drop=True))
        tv = normalize_missing(tv)
        changed = ~_equal_mask(bv, tv) & pd.Series(matched)
        col_kinds, col_labels, note = classify_column(bv, tv, changed)

        idx = changed[changed].index.to_numpy()
        # Pandas mask-assignment coerces None -> NaN in object Series; normalize
        # back so downstream `is not None` checks and JSON stay clean.
        col_labels = col_labels.where(col_labels.notna(), None)
        kinds[idx, j] = col_kinds[idx].to_numpy()
        labels[idx, j] = col_labels[idx].to_numpy()
        olds[idx, j] = bv[idx].to_numpy(dtype=object)
        spec["changed_cells"] = int(changed.sum())
        if note and spec["note"] is None:
            spec["note"] = note
        cells_changed += spec["changed_cells"]
        columns_spec.append(spec)

    # Rows count as "changed" from cell edits in shared columns or being new;
    # a wholly-added column tints its cells but shouldn't flag every row.
    added_col_mask = np.array([spec["status"] == "added" for spec in columns_spec], dtype=bool)
    cell_changed = kinds != None  # noqa: E711
    if added_col_mask.any():
        cell_changed = cell_changed[:, ~added_col_mask]
    changed_rows = rows_added | cell_changed.any(axis=1)

    kinds[rows_added, :] = "added"
    labels[rows_added, :] = None

    # Ghost rows: base rows with no counterpart in the target, appended after
    # the target rows so removed records stay visible in the comparison.
    values = target.reset_index(drop=True)
    matched_positions = row_base_pos[row_base_pos >= 0]
    removed_positions = np.setdiff1d(np.arange(len(base)), matched_positions)
    ghost_count = len(removed_positions)
    if ghost_count:
        ghost_data = {}
        for col in display_cols:
            bcol = renames.get(col, col)
            if bcol in base.columns:
                ghost_data[col] = base[bcol].iloc[removed_positions].reset_index(drop=True)
            else:
                ghost_data[col] = pd.Series([None] * ghost_count, dtype=object)
        values = pd.concat([values, pd.DataFrame(ghost_data)], ignore_index=True)
        kinds = np.vstack(
            [kinds, np.full((ghost_count, len(display_cols)), "removed", dtype=object)]
        )
        labels = np.vstack([labels, np.full((ghost_count, len(display_cols)), None, dtype=object)])
        olds = np.vstack([olds, np.full((ghost_count, len(display_cols)), None, dtype=object)])
        changed_rows = np.concatenate([changed_rows, np.ones(ghost_count, dtype=bool)])
        rows_added = np.concatenate([rows_added, np.zeros(ghost_count, dtype=bool)])

    ghost_mask = np.zeros(len(values), dtype=bool)
    ghost_mask[n_rows:] = True
    row_kind_masks = {
        "added": rows_added,
        "removed": ghost_mask | (kinds == "removed").any(axis=1),
        "changed": (kinds == "changed").any(axis=1),
        "imputed": (kinds == "imputed").any(axis=1),
        "transformed": (kinds == "transformed").any(axis=1),
    }

    n_base_cols = max(len(base.columns), 1)
    n_target_cols = max(len(target.columns), 1)
    overlap = len(common) + len(renames)
    similarity = overlap / max(n_base_cols, n_target_cols)
    warning = None
    if overlap == 0:
        warning = (
            "These two sources share no columns — they do not appear to be "
            "versions of the same dataset."
        )
    elif similarity < LOW_SIMILARITY_THRESHOLD:
        warning = (
            f"Low overlap: only {overlap} of {max(n_base_cols, n_target_cols)} "
            "columns are shared. The comparison may not be meaningful."
        )

    summary = {
        "rows_added": int(rows_added.sum()),
        "rows_removed": rows_removed,
        "rows_changed": int((changed_rows & ~rows_added & ~ghost_mask).sum()),
        "rows_unchanged": int((~changed_rows).sum()),
        "cells_changed": cells_changed,
        "columns_added": [str(c) for c in added_cols],
        "columns_removed": [str(c) for c in removed_cols],
        "columns_renamed": {str(new): str(old) for new, old in renames.items()},
        "similarity": round(similarity, 3),
        "alignment": alignment,
        "warning": warning,
    }

    return DiffResult(
        columns=columns_spec,
        values=values,
        kinds=kinds,
        labels=labels,
        olds=olds,
        changed_rows=changed_rows,
        ghost_rows=ghost_mask,
        row_kind_masks=row_kind_masks,
        summary=summary,
    )


# --------------------------------------------------------------- cache + page


def get_cached_diff(
    cache_key: Optional[Tuple], base: pd.DataFrame, target: pd.DataFrame
) -> DiffResult:
    """Compute (or fetch) the diff; only file-backed pairs are cacheable."""
    if cache_key is not None:
        hit = _diff_cache.get(cache_key)
        if hit is not None:
            _diff_cache.move_to_end(cache_key)
            return hit
    result = compute_diff(base, target)
    if cache_key is not None:
        _diff_cache[cache_key] = result
        while len(_diff_cache) > _CACHE_MAX_ENTRIES:
            _diff_cache.popitem(last=False)
    return result


def clear_diff_cache() -> None:
    _diff_cache.clear()


def build_diff_page(
    result: DiffResult,
    *,
    page: int = 1,
    page_size: int = 100,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
) -> Dict[str, Any]:
    df = result.values
    selected = np.ones(len(df), dtype=bool)
    if changes_only:
        selected &= result.changed_rows
    if change_kind:
        selected &= result.row_kind_masks.get(change_kind, np.zeros(len(df), dtype=bool))

    by_name = {str(col): col for col in df.columns}
    for name, needle in (filters or {}).items():
        col = by_name.get(name)
        if col is None or not needle:
            continue
        selected &= _contains_mask(df[col], needle).to_numpy()
    if search:
        search_mask = np.zeros(len(df), dtype=bool)
        for col in df.columns:
            search_mask |= _contains_mask(df[col], search).to_numpy()
        selected &= search_mask

    row_index = np.flatnonzero(selected)
    sort_col = by_name.get(sort_by) if sort_by else None
    if sort_col is not None and len(row_index):
        sub = df.iloc[row_index]
        key = (lambda s: s.astype(str)) if pd.api.types.is_object_dtype(df[sort_col]) else None
        ordered = sub.sort_values(
            sort_col,
            ascending=(sort_dir != "desc"),
            kind="stable",
            na_position="last",
            key=key,
        )
        row_index = ordered.index.to_numpy()

    filtered_rows = len(row_index)
    total_pages = max(1, ceil(filtered_rows / page_size))
    page = min(max(1, page), total_pages)
    page_rows = row_index[(page - 1) * page_size : page * page_size]

    page_df = result.values.iloc[page_rows]
    rows = [[to_json_safe(v) for v in row] for row in page_df.itertuples(index=False, name=None)]

    cell_meta: List[List[Optional[Dict[str, Any]]]] = []
    for i in page_rows:
        row_meta: List[Optional[Dict[str, Any]]] = []
        for j in range(len(result.columns)):
            kind = result.kinds[i, j]
            if kind is None:
                row_meta.append(None)
                continue
            meta: Dict[str, Any] = {"k": kind}
            if result.labels[i, j] is not None:
                meta["l"] = result.labels[i, j]
            # Ghost rows carry no "o" — the frontend reads its absence on a
            # "removed" cell as "this whole row is not in the target".
            if result.olds[i, j] is not None:
                meta["o"] = to_json_safe(result.olds[i, j])
            elif kind != "added" and not result.ghost_rows[i]:
                meta["o"] = None
            row_meta.append(meta)
        cell_meta.append(row_meta)

    return {
        "kind": "diff",
        "columns": result.columns,
        "rows": rows,
        "cell_meta": cell_meta,
        "total_rows": len(result.values),
        "filtered_rows": filtered_rows,
        "page": page,
        "total_pages": total_pages,
        "summary": result.summary,
    }

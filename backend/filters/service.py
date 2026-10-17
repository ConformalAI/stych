"""
Pandas engine for applying a FilterSpec: resolve row identity, run annotations,
compile the select/present stages, and serve one page shaped like the diff
engine's output (rows + cell_meta + the new row_meta channel + summary), so the
grid renders filters and diffs through the same channels.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from filters.annotate_pandas import AnnotationResult
from filters.predicate_pandas import UnknownColumn, compile_mask
from filters.spec import SEVERITY_ORDER, FilterSpec, PresentRule

_SEVERITY_RANK = {sev: i for i, sev in enumerate(SEVERITY_ORDER)}
_ID_LIKE = ("id", "index", "key")


class AnnotationCollision(ValueError):
    """An annotation output name collides with a base column."""

    def __init__(self, column: str) -> None:
        super().__init__(f"annotation output {column!r} collides with a base column")
        self.column = column


# ------------------------------------------------------------------ identity


def resolve_row_identity(df: pd.DataFrame, identity) -> Tuple[pd.Series, str, Optional[str]]:
    """Resolve the spec's RowIdentity against live data.

    Returns (row_keys, resolved_label, warning). Row keys are the key column's
    values, or positions. An explicit key that fails the unique+non-null probe
    falls back to positional with a warning rather than erroring — the filter
    should still render.
    """

    def _usable(col: str) -> bool:
        series = df[col]
        if series.isna().any():
            return False
        try:
            return series.nunique() == len(series)
        except TypeError:  # unhashable cells
            return False

    positional = pd.Series(np.arange(len(df)))

    if identity.mode == "positional":
        return positional, "positional", None

    if identity.mode == "key":
        if identity.key in df.columns and _usable(identity.key):
            return df[identity.key].reset_index(drop=True), f"key:{identity.key}", None
        return (
            positional,
            "positional",
            f"key column {identity.key!r} is missing or not unique; using row position",
        )

    # auto: prefer id-like names among unique+non-null candidates.
    candidates = [str(c) for c in df.columns if _usable(str(c))]
    for col in candidates:
        low = col.lower()
        if low in _ID_LIKE or low.endswith("_id"):
            return df[col].reset_index(drop=True), f"key:{col}", None
    if candidates:
        col = candidates[0]
        return df[col].reset_index(drop=True), f"key:{col}", None
    return positional, "positional", None


# ------------------------------------------------------------------ helpers


class _RowFormat(dict):
    """format_map dict that leaves unknown {placeholders} intact."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_tooltip(template: str, row_values: Dict[str, Any]) -> str:
    try:
        return template.format_map(_RowFormat(row_values))[:200]
    except (ValueError, KeyError):  # bad format spec on a non-numeric value etc.
        return template[:200]


def _rule_mask(
    rule: PresentRule,
    select_mask: pd.Series,
    resolve,
) -> Tuple[Optional[pd.Series], Optional[str]]:
    """Compile one rule's predicate; unknown columns skip the rule (not the page)."""
    if rule.predicate is None:
        return select_mask, None
    try:
        return compile_mask(rule.predicate, resolve), None
    except UnknownColumn as exc:
        return None, f"rule {rule.id!r} references unknown column {exc.column!r}"


def _group_sort_index(groups: pd.Series) -> np.ndarray:
    """Row order: grouped rows first (groups in first-occurrence order, members
    adjacent in original order), ungrouped rows after in original order."""
    first_seen: Dict[Any, int] = {}
    ordinals = np.full(len(groups), np.inf)
    for pos, val in enumerate(groups.to_numpy()):
        if pd.isna(val):
            continue
        if val not in first_seen:
            first_seen[val] = len(first_seen)
        ordinals[pos] = first_seen[val]
    return np.lexsort((np.arange(len(groups)), ordinals))


# ------------------------------------------------------------------ page


def build_filter_page(
    df: pd.DataFrame,
    spec: FilterSpec,
    ann: AnnotationResult,
    *,
    identity_label: str,
    identity_warning: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
    matches_only: Optional[bool] = None,
    enabled_rules: Optional[List[str]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Apply select/present over the annotated frame and serialize one page.

    Raises UnknownColumn when the *select* predicate references a missing
    column (caller maps to 400); rule-level unknowns skip that rule only.
    """
    # Local imports: api.preview_service transitively reaches the agents
    # package, which imports this module (FilterAgent) — a top-level import
    # would be circular.
    from api.preview_service import _contains_mask, column_kind, column_specs  # noqa: PLC0415
    from helpers.helper import to_json_safe  # noqa: PLC0415

    base = df.reset_index(drop=True)
    ann_frame = ann.frame.reset_index(drop=True)
    collisions = [c for c in ann_frame.columns if c in base.columns]
    if collisions:
        raise AnnotationCollision(str(collisions[0]))
    display = pd.concat([base, ann_frame], axis=1)
    n = len(display)

    columns_by_name = {str(c): display[c] for c in display.columns}

    def resolve(name: str) -> pd.Series:
        series = columns_by_name.get(name)
        if series is None:
            raise UnknownColumn(name)
        return series

    select_mask = (
        compile_mask(spec.select, resolve)
        if spec.select is not None
        else pd.Series(True, index=display.index)
    )

    warnings: List[str] = [w for w in (identity_warning,) if w]
    rule_masks: Dict[str, pd.Series] = {}
    for rule in spec.present.rules:
        mask, warn = _rule_mask(rule, select_mask, resolve)
        if warn:
            warnings.append(warn)
            continue
        rule_masks[rule.id] = mask

    # ---- row severity / labels / tooltips (row-target rules) ----
    sev_rank = np.full(n, np.inf)
    row_sev = np.full(n, None, dtype=object)
    row_label = np.full(n, None, dtype=object)
    row_tooltip_rule = np.full(n, None, dtype=object)
    for rule in spec.present.rules:
        mask = rule_masks.get(rule.id)
        if mask is None or rule.target != "row":
            continue
        rank = _SEVERITY_RANK[rule.severity]
        hit = mask.to_numpy() & (rank < sev_rank)
        sev_rank[hit] = rank
        row_sev[hit] = rule.severity
        row_label[hit] = rule.label
        if rule.tooltip:
            row_tooltip_rule[hit] = rule.tooltip

    # ---- narrowing ----
    effective_matches_only = spec.present.matches_only if matches_only is None else matches_only
    selected = np.ones(n, dtype=bool)
    if effective_matches_only:
        selected &= select_mask.to_numpy()
    if enabled_rules is not None:
        union = np.zeros(n, dtype=bool)
        for rule_id in enabled_rules:
            mask = rule_masks.get(rule_id)
            if mask is not None:
                union |= mask.to_numpy()
        selected &= union

    for name, needle in (filters or {}).items():
        series = columns_by_name.get(name)
        if series is None or not needle:
            continue
        selected &= _contains_mask(series, needle).to_numpy()
    if search:
        search_mask = np.zeros(n, dtype=bool)
        for series in columns_by_name.values():
            search_mask |= _contains_mask(series, search).to_numpy()
        selected &= search_mask

    # ---- ordering: grouping owns the order; else user sort ----
    group_col = spec.present.group_by
    groups = display[group_col] if group_col and group_col in display.columns else None
    if groups is not None:
        order = _group_sort_index(groups)
        row_index = order[selected[order]]
    else:
        row_index = np.flatnonzero(selected)
        label_by_name = {str(c): c for c in display.columns}
        sort_label = label_by_name.get(sort_by) if sort_by else None
        if sort_label is not None and len(row_index):
            sub = display.iloc[row_index]
            key = (
                (lambda s: s.astype(str))
                if pd.api.types.is_object_dtype(display[sort_label])
                else None
            )
            ordered = sub.sort_values(
                sort_label,
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
    page_df = display.iloc[page_rows]

    rows = [[to_json_safe(v) for v in row] for row in page_df.itertuples(index=False, name=None)]

    # Remap group values to per-result ordinals so the grid can band by parity.
    group_ordinal: Dict[Any, int] = {}
    display_names = [str(c) for c in display.columns]
    col_pos = {name: j for j, name in enumerate(display_names)}

    cell_meta: List[List[Optional[Dict[str, Any]]]] = []
    row_meta: List[Optional[Dict[str, Any]]] = []
    matched_np = select_mask.to_numpy()

    cells_rules = [
        (rule, rule_masks[rule.id])
        for rule in spec.present.rules
        if rule.target == "cells" and rule.id in rule_masks
    ]

    for i in page_rows:
        row_values = {name: to_json_safe(display.iat[i, j]) for name, j in col_pos.items()}

        meta_cells: List[Optional[Dict[str, Any]]] = [None] * len(display_names)
        for rule, mask in cells_rules:
            if not mask.iat[i]:
                continue
            rank = _SEVERITY_RANK[rule.severity]
            for col in rule.columns or []:
                j = col_pos.get(col)
                if j is None:
                    continue
                existing = meta_cells[j]
                if existing is not None and _SEVERITY_RANK[existing["k"]] <= rank:
                    continue
                cell: Dict[str, Any] = {"k": rule.severity}
                if rule.label:
                    cell["l"] = rule.label
                if rule.tooltip:
                    cell["t"] = _render_tooltip(rule.tooltip, row_values)
                meta_cells[j] = cell
        cell_meta.append(meta_cells)

        group_val = groups.iat[i] if groups is not None else None
        has_group = group_val is not None and not pd.isna(group_val)
        if has_group and group_val not in group_ordinal:
            group_ordinal[group_val] = len(group_ordinal)

        if not (has_group or row_sev[i] is not None or matched_np[i]):
            row_meta.append(None)
            continue
        entry: Dict[str, Any] = {"m": bool(matched_np[i])}
        if has_group:
            entry["g"] = group_ordinal[group_val]
        if row_sev[i] is not None:
            entry["s"] = row_sev[i]
        if row_label[i] is not None:
            entry["l"] = row_label[i]
        if row_tooltip_rule[i] is not None:
            entry["t"] = _render_tooltip(row_tooltip_rule[i], row_values)
        row_meta.append(entry)

    # ---- columns + summary ----
    columns = column_specs(base)
    for col in ann_frame.columns:
        columns.append(
            {
                "name": str(col),
                "dtype": str(ann_frame[col].dtype),
                "kind": column_kind(ann_frame[col].dtype),
                "origin": "annotation",
            }
        )

    rule_counts = {rid: int(mask.sum()) for rid, mask in rule_masks.items()}
    group_count = int(groups.nunique(dropna=True)) if groups is not None else None
    # Rows carrying a non-null group value that don't match `select`: a group_by column is only
    # meaningful over matches, so any orphan here means the annotate step used a sentinel (e.g. -1)
    # instead of null/NaN for non-members, which silently breaks group_by banding.
    group_orphan_rows = (
        int((groups.notna().to_numpy() & ~matched_np).sum()) if groups is not None else None
    )
    degraded = ann.degraded
    if identity_label == "positional" and any(s["status"] == "skipped" for s in ann.statuses):
        degraded = degraded or "no_key_annotations"

    summary = {
        "matched_rows": int(matched_np.sum()),
        "total_rows": n,
        "approx": ann.approx,
        "rule_counts": rule_counts,
        "group_count": group_count,
        "group_orphan_rows": group_orphan_rows,
        "identity_resolved": identity_label,
        "degraded": degraded,
        "warning": "; ".join(warnings) if warnings else None,
        "annotations": ann.statuses,
        "group_by": group_col,
    }

    return {
        "kind": "filter",
        "columns": columns,
        "rows": rows,
        "cell_meta": cell_meta,
        "row_meta": row_meta,
        "total_rows": n,
        "filtered_rows": filtered_rows,
        "page": page,
        "total_pages": total_pages,
        "summary": summary,
    }

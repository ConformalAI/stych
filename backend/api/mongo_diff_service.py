"""
Document diff for MongoDB sources: compares a pinned base collection against
the working copy inside the customer's own cluster, so whole collections diff
without materializing either side locally.

Shape-compatible with ``diff_service.build_diff_page`` (the frontend cannot tell
the engines apart), but three things differ, all from the data model:

- **Alignment is always ``_id``.** MongoDB guarantees it and ``$out`` preserves
  it, so there is no key detection and no keyless degradation — the one place
  documents are strictly easier to diff than rows.
- **Change detection is whole-document**, via a set comparison of each
  document's top-level fields. That covers fields absent from the sampled
  schema, which a per-column comparison would silently skip. The cost is that
  reordering a *nested* subdocument's fields reads as a change; erring toward
  "flagged" beats missing a real edit.
- **Comparison flattens deeper than display.** The grid shows one ``tags``
  column, but a change inside it is attributed to ``tags.1`` in the cell
  tooltip, so a nested edit is legible rather than "this object changed".

Both sides must live in the same database: ``$lookup`` cannot cross databases.
That is what the pinned base already guarantees — it and the working copy are
both scratch collections.
"""

from __future__ import annotations

import logging
from math import ceil
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from api.diff_detectors import classify_column, is_missing_value, normalize_missing
from api.diff_service import _equal_mask
from api.mongo_diff_pipelines import (
    ADDED,
    BASE,
    CHANGED,
    INTERNAL,
    REMOVED,
    build_added_pipeline,
    build_aligned_pipeline,
    build_cell_counts_pipeline,
    build_summary_pipeline,
)
from api.preview_service import column_kind
from helpers.helper import to_json_safe

logger = logging.getLogger(__name__)

#: Documents sampled to infer display columns and to classify transforms.
SCHEMA_SAMPLE = 1_000
CLASSIFY_SAMPLE = 2_000


class MongoDiffError(ValueError):
    """The two sides cannot be compared as given."""


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def deep_flatten(value: Any, prefix: str = "") -> Dict[str, Any]:
    """Every leaf in a document, keyed by dotted path.

    Arrays are indexed positionally (``tags.0``), which makes an edit legible
    but reports an insertion as a change to every later element. Keyed array
    alignment would fix that and is a refinement, not a correction — positional
    indices are what the display flattening already implies.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            out.update(deep_flatten(item, f"{prefix}{key}."))
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for index, item in enumerate(value):
            out.update(deep_flatten(item, f"{prefix}{index}."))
        return out
    return {prefix.rstrip("."): value} if prefix else {}


def _changed_subpaths(base: Any, target: Any, limit: int = 6) -> List[str]:
    """Which leaf paths differ inside one display value."""
    b, t = deep_flatten(base), deep_flatten(target)
    names = sorted(set(b) | set(t))
    return [name for name in names if b.get(name) != t.get(name)][:limit]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------


def _contains(column: str, needle: str) -> Dict[str, Any]:
    import re  # noqa: PLC0415

    text = {"$convert": {"input": f"${column}", "to": "string", "onError": "", "onNull": ""}}
    return {"$regexMatch": {"input": text, "regex": re.escape(str(needle)), "options": "i"}}


def build_narrow_expr(
    columns: Sequence[str],
    *,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """The grid's narrowing, applied to both the page and the filtered count."""
    known = set(columns)
    terms: List[Any] = []
    if changes_only:
        terms.append({"$or": [f"${ADDED}", f"${REMOVED}", f"${CHANGED}"]})
    if change_kind == "added":
        terms.append(f"${ADDED}")
    elif change_kind == "removed":
        terms.append(f"${REMOVED}")
    elif change_kind == "changed":
        terms.append(f"${CHANGED}")
    for name, needle in (filters or {}).items():
        if name in known and needle:
            terms.append(_contains(name, needle))
    if search and columns:
        terms.append({"$or": [_contains(c, search) for c in columns]})
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else {"$and": terms}


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------


def _sides(doc: Dict[str, Any]) -> Tuple[Optional[Dict], Optional[Dict], str]:
    """Split one aligned document into ``(base, target, kind)``."""
    body = {k: v for k, v in doc.items() if k not in INTERNAL and k != "__t"}
    if doc.get(REMOVED):
        return body, None, "removed"
    if doc.get(ADDED):
        return None, body, "added"
    matched = doc.get(BASE) or []
    base = {k: v for k, v in (matched[0] if matched else {}).items() if k not in INTERNAL}
    return base, body, ("changed" if doc.get(CHANGED) else "unchanged")


def _display_values(doc: Optional[Dict[str, Any]], columns: Sequence[str]) -> Dict[str, Any]:
    """One document as the grid shows it: the preview's flattening, not the
    deeper comparison flattening."""
    if doc is None:
        return {}
    from datasources.mongodb import flatten_documents  # noqa: PLC0415

    frame = flatten_documents([doc])
    if frame.empty:
        return {}
    record = frame.iloc[0]
    return {name: record[name] for name in columns if name in frame.columns}


def _classify(pairs: List[Tuple[Dict, Dict]], columns: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Column-wise transform labels, from a bounded sample of changed pairs.

    Reuses the pandas detectors so a MongoDB diff labels a standardization or a
    log transform with the same words the other two engines use.
    """
    if not pairs:
        return {}
    base_frame = pd.DataFrame([_display_values(b, columns) for b, _ in pairs])
    target_frame = pd.DataFrame([_display_values(t, columns) for _, t in pairs])
    out: Dict[str, Dict[str, Any]] = {}
    for name in columns:
        if name not in base_frame.columns or name not in target_frame.columns:
            continue
        bv = normalize_missing(base_frame[name].reset_index(drop=True))
        tv = normalize_missing(target_frame[name].reset_index(drop=True))
        changed = ~_equal_mask(bv, tv)
        if not changed.any():
            continue
        kinds, labels, _note = classify_column(bv, tv, changed)
        labels = labels.where(labels.notna(), None)

        def _mode(series: pd.Series) -> Optional[str]:
            values = series.dropna()
            return str(values.mode().iloc[0]) if len(values) else None

        out[name] = {
            "kind": _mode(kinds[changed & bv.notna()]) or "changed",
            "label": _mode(labels[changed & bv.notna()].astype(object)),
            "imputed_label": _mode(labels[changed & bv.isna()].astype(object)),
        }
    return out


def _cell_meta(
    base: Optional[Dict[str, Any]],
    target: Optional[Dict[str, Any]],
    columns: Sequence[str],
    kind: str,
    classes: Dict[str, Dict[str, Any]],
) -> List[Optional[Dict[str, Any]]]:
    if kind == "removed":
        # No "o": the frontend reads its absence on a removed cell as "this
        # whole row is not present in the target".
        return [{"k": "removed"} for _ in columns]
    if kind == "added":
        return [{"k": "added"} for _ in columns]

    base_values = _display_values(base, columns)
    target_values = _display_values(target, columns)
    meta: List[Optional[Dict[str, Any]]] = []
    for name in columns:
        old, new = base_values.get(name), target_values.get(name)
        if _same(old, new):
            meta.append(None)
            continue
        klass = classes.get(name, {})
        if is_missing_value(old):
            cell: Dict[str, Any] = {"k": "imputed", "o": None}
            if klass.get("imputed_label"):
                cell["l"] = klass["imputed_label"]
        else:
            cell = {"k": klass.get("kind") or "changed", "o": to_json_safe(old)}
            if klass.get("label"):
                cell["l"] = klass["label"]
        # Where the display value is a nested structure, say which leaf moved.
        sub = _changed_subpaths((base or {}).get(name), (target or {}).get(name))
        if sub:
            cell["p"] = sub
        meta.append(cell)
    return meta


def _same(old: Any, new: Any) -> bool:
    if is_missing_value(old) and is_missing_value(new):
        return True
    return bool(pd.Series([old]).equals(pd.Series([new])))


def build_mongo_diff_page(
    runner,
    *,
    database: str,
    base_collection: str,
    target_collection: str,
    page: int = 1,
    page_size: int = 100,
    changes_only: bool = False,
    change_kind: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
) -> Dict[str, Any]:
    """Compare two collections in the same database, in the shared diff shape."""
    base_schema = runner.get_table_columns(database, base_collection, sample=SCHEMA_SAMPLE)
    target_schema = runner.get_table_columns(database, target_collection, sample=SCHEMA_SAMPLE)
    base_names = [c["column_name"] for c in base_schema]
    target_names = [c["column_name"] for c in target_schema]
    base_types = {c["column_name"]: str(c.get("data_type") or "object") for c in base_schema}
    target_types = {c["column_name"]: str(c.get("data_type") or "object") for c in target_schema}
    # Target order leads, with removed columns kept so their loss is visible.
    columns = list(target_names) + [n for n in base_names if n not in set(target_names)]
    added_columns = [n for n in target_names if n not in set(base_names)]
    removed_columns = [n for n in base_names if n not in set(target_names)]

    stats = runner.run_pipeline(
        base_collection,
        build_summary_pipeline(base_collection, target_collection),
        database=database,
    )
    agg = stats[0] if stats else {}
    base_total = int(agg.get("base_total") or 0)
    rows_removed = int(agg.get("rows_removed") or 0)
    rows_changed = int(agg.get("rows_changed") or 0)
    added_rows = runner.run_pipeline(
        target_collection, build_added_pipeline(base_collection), database=database
    )
    rows_added = int(added_rows[0]["n"]) if added_rows else 0
    rows_unchanged = max(0, base_total - rows_removed - rows_changed)
    total_rows = base_total + rows_added

    cell_counts_result = runner.run_pipeline(
        target_collection,
        build_cell_counts_pipeline(base_collection, target_collection, columns),
        database=database,
    )
    raw_cell_counts = cell_counts_result[0] if cell_counts_result else {}
    changed_cells_by_column = {
        name: int(raw_cell_counts.get(f"c{index}") or 0) for index, name in enumerate(columns)
    }

    aligned = build_aligned_pipeline(base_collection, target_collection)
    narrow = build_narrow_expr(
        columns,
        changes_only=changes_only,
        change_kind=change_kind,
        search=search,
        filters=filters,
    )
    if narrow is None:
        filtered = total_rows
    else:
        aligned = aligned + [{"$match": {"$expr": narrow}}]
        counted = runner.run_pipeline(
            target_collection, aligned + [{"$count": "n"}], database=database
        )
        filtered = int(counted[0]["n"]) if counted else 0

    total_pages = max(1, ceil(filtered / page_size)) if filtered else 1
    page = min(max(1, page), total_pages)
    order = -1 if sort_dir == "desc" else 1
    sort: Dict[str, Any] = (
        {sort_by: order, "_id": 1} if sort_by and sort_by in set(columns) else {"_id": 1}
    )
    documents = runner.run_pipeline(
        target_collection,
        aligned + [{"$sort": sort}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}],
        database=database,
    )

    split = [_sides(doc) for doc in documents]
    classes = _classify(
        [(b, t) for b, t, kind in split if kind == "changed" and b and t][:CLASSIFY_SAMPLE],
        columns,
    )

    rows: List[List[Any]] = []
    cell_meta: List[List[Optional[Dict[str, Any]]]] = []
    for base, target, kind in split:
        shown = _display_values(target if kind != "removed" else base, columns)
        rows.append([to_json_safe(shown.get(name)) for name in columns])
        cell_meta.append(_cell_meta(base, target, columns, kind, classes))

    similarity = round(rows_unchanged / total_rows, 3) if total_rows else 1.0
    return {
        "kind": "diff",
        "columns": [
            {
                "name": name,
                "dtype": target_types.get(name) or base_types.get(name) or "object",
                "kind": column_kind(target_types.get(name) or base_types.get(name) or "object"),
                "status": _column_status(name, added_columns, removed_columns),
                "note": None,
                "changed_cells": changed_cells_by_column[name],
            }
            for name in columns
        ],
        "rows": rows,
        "cell_meta": cell_meta,
        "total_rows": total_rows,
        "filtered_rows": filtered,
        "page": page,
        "total_pages": total_pages,
        "summary": {
            "rows_added": rows_added,
            "rows_removed": rows_removed,
            "rows_changed": rows_changed,
            "rows_unchanged": rows_unchanged,
            "cells_changed": sum(changed_cells_by_column.values()),
            "columns_added": added_columns,
            "columns_removed": removed_columns,
            # Renames are detected by value similarity across a positional
            # sample, which needs an ordering documents do not have. Left empty
            # rather than guessed.
            "columns_renamed": {},
            "similarity": similarity,
            # MongoDB guarantees _id and $out preserves it, so alignment never
            # degrades the way a keyless SQL table does.
            "alignment": "key:_id",
            "warning": (
                "Columns are inferred from a sample of documents; a field that appears "
                "only in unsampled documents is not shown. Whole-document comparison "
                "still counts changes to those fields."
            ),
        },
    }


def _column_status(name: str, added: Sequence[str], removed: Sequence[str]) -> str:
    if name in added:
        return "added"
    if name in removed:
        return "removed"
    return "common"


def resolve_diff_target(meta: Dict[str, Any], variant: str, scratch_db: str) -> Tuple[str, str]:
    """A diff side as ``(database, collection)``.

    ``base`` resolves to the *pinned* snapshot rather than the live collection.
    Both sides must sit in the scratch database because ``$lookup`` cannot cross
    databases — and the pin is what makes the comparison exact anyway: the live
    collection can move under a running diff, a pinned copy cannot.
    """
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415

    if variant == "temp":
        collection = meta.get("temp_table")
        if not collection:
            raise MongoDiffError(
                "No working copy exists yet — it is created when an agent first writes."
            )
        return str(meta.get("temp_schema") or scratch_db), str(collection)

    if variant.startswith("copy:"):
        entry = wc.get_copy(meta, variant[len("copy:") :])
        if entry is None:
            raise MongoDiffError(f"No working copy named '{variant[len('copy:'):]}' exists.")
        return scratch_db, str(entry["table"])

    if variant == "base":
        pinned = wc.pinned_base(meta)
        if pinned is None:
            raise MongoDiffError(
                "Comparing versions needs a pinned snapshot of the collection, and none "
                "was taken for this session. It is created the first time an agent writes."
            )
        return scratch_db, str(pinned["table"])

    raise MongoDiffError(f"Unknown source variant '{variant}'.")

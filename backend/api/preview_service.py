"""
Data preview service: loads workspace files / in-memory datasets and runs the
pagination, filter, sort, and serialization pipeline for the preview endpoints.

Pure logic lives here (unit-testable without the app); HTTP wiring is in
``api/preview_routes.py``.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from contextlib import nullcontext
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from fastapi import HTTPException

from api.cleaning_export_routes import _resolve_workspace_path
from api.source_lifecycle import raise_source_removed
from helpers.helper import to_json_safe
from workspace.manager import WorkspaceManager

MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024
MAX_LOAD_ROWS = 100_000
MAX_TEXT_BYTES = 256 * 1024

TABLE_SUFFIXES = {"csv", "tsv", "xlsx", "parquet"}
TEXT_SUFFIXES = {"json", "txt", "md"}
PREVIEWABLE_SUFFIXES = TABLE_SUFFIXES | TEXT_SUFFIXES
# Parquet/csv/tsv page through DuckDB. xlsx without a parquet sibling stays pandas.
FILE_PUSHDOWN_SUFFIXES = {"parquet", "csv", "tsv"}

# Tiny LRU keyed by (path, mtime_ns): amortizes re-parsing (mainly xlsx) across
# page requests; mtime in the key invalidates entries when agents rewrite files.
_CACHE_MAX_ENTRIES = 4
_frame_cache: "OrderedDict[Tuple[str, int], Dict[str, Any]]" = OrderedDict()


def _cache_entry(path: Path) -> Dict[str, Any]:
    key = (str(path), path.stat().st_mtime_ns)
    entry = _frame_cache.get(key)
    if entry is not None:
        _frame_cache.move_to_end(key)
        return entry
    df, truncated = _read_table_file(path)
    entry = {"df": df, "truncated": truncated, "stats": None}
    _frame_cache[key] = entry
    while len(_frame_cache) > _CACHE_MAX_ENTRIES:
        _frame_cache.popitem(last=False)
    return entry


def _read_table_file(path: Path) -> Tuple[pd.DataFrame, bool]:
    """Load a workspace table file.

    Parquet/csv/tsv are read in full so Compare (and filter fallback) see the
    same rows as DuckDB Data-tab paging. xlsx has no pushdown path and stays
    row-capped.
    """
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("csv", "tsv"):
        sep = "\t" if suffix == "tsv" else ","
        return pd.read_csv(path, sep=sep), False
    if suffix == "xlsx":
        df = pd.read_excel(path, sheet_name=0)
        truncated = len(df) > MAX_LOAD_ROWS
        if truncated:
            df = df.head(MAX_LOAD_ROWS)
        return df, truncated
    if suffix == "parquet":
        return pd.read_parquet(path), False
    raise HTTPException(status_code=400, detail="This file type cannot be previewed.")


def load_table_file(path: Path) -> Dict[str, Any]:
    """Load a tabular workspace file with size guard and LRU cache.

    Returns the cache entry ``{"df", "truncated", "stats"}``.
    """
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File is too large to preview.")
    return _cache_entry(path)


def read_text_file(path: Path) -> Dict[str, Any]:
    """Read a text/json/md file capped at MAX_TEXT_BYTES."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    size_bytes = path.stat().st_size
    with open(path, "rb") as f:
        raw = f.read(MAX_TEXT_BYTES)
    return {
        "kind": "text",
        "text": raw.decode("utf-8", errors="replace"),
        "size_bytes": size_bytes,
        "truncated": size_bytes > MAX_TEXT_BYTES,
    }


def parse_filters_param(filters: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse the grid's JSON ``filters`` query param ({"col": "substring"})."""
    if not filters:
        return None
    try:
        parsed = json.loads(filters)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid filters parameter.")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Invalid filters parameter.")
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def resolve_sql_source_id(session, source_id: Optional[str]):
    """Parse ``"<file_id>::base|temp|copy:<name>"`` and return (ctx, variant)."""
    if not source_id:
        raise HTTPException(status_code=400, detail="Missing source_id.")
    file_id, sep, variant = source_id.rpartition("::")
    if not sep or not (variant in ("base", "temp", "working") or variant.startswith("copy:")):
        raise HTTPException(status_code=400, detail="Invalid source_id.")
    ctx = next(
        (c for c in session.get_all_data_contexts() if c.file_id == file_id),
        None,
    )
    if ctx is None:
        raise_source_removed()
    return ctx, variant


def lossless_source(target: Path) -> Path:
    """Prefer a same-stem ``.parquet`` when reading a table file. Parquet preserves
    dtypes that CSV loses on round-trip — e.g. a numeric column cast to string
    reads back from CSV as numbers (``pd.read_csv`` re-infers), which would hide
    the recast in the diff. Listing now points at parquet; this still upgrades
    leftover csv/xlsx siblings."""
    if target.suffix.lower() == ".parquet":
        return target
    sibling = target.with_suffix(".parquet")
    return sibling if sibling.is_file() else target


def build_file_page(
    session_id: str,
    path: Path,
    workspace: Path,
    *,
    page: int = 1,
    page_size: int = 100,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    include_stats: bool = False,
) -> Dict[str, Any]:
    """Page a workspace parquet/csv/tsv via DuckDB without loading it into pandas."""
    from datasources.adapters.file_preview import open_file_preview_adapter

    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File is too large to preview.")
    with open_file_preview_adapter(session_id, path, allowed_root=workspace) as adapter:
        return build_adapter_page(
            adapter,
            session_id=session_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
            filters=filters,
            include_stats=include_stats,
        )


def load_tabular_source(session, session_id: str, user: str, source: str, path, file_id):
    """Resolve a tabular source to (DataFrame, cache-key-part). Datasets are
    in-memory (no cache key); files key on (path, mtime)."""
    if source == "dataset":
        if not file_id:
            raise HTTPException(status_code=400, detail="Missing file_id.")
        ctx = next(
            (c for c in session.get_all_data_contexts() if c.file_id == file_id),
            None,
        )
        if ctx is None:
            raise_source_removed()
        return ctx.dataframe, None

    if not path:
        raise HTTPException(status_code=400, detail="Missing path.")
    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    target = _resolve_workspace_path(workspace, path)
    suffix = target.suffix.lower().lstrip(".")
    if suffix not in TABLE_SUFFIXES:
        raise HTTPException(status_code=400, detail="This file type cannot be compared.")
    target = lossless_source(target)
    entry = load_table_file(target)
    return entry["df"], (str(target), target.stat().st_mtime_ns)


def column_kind(dtype: Any) -> str:
    """Simplified column type used by the frontend for cell formatting."""
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date"
    if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
        return "string"
    return "other"


def column_specs(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        {"name": str(col), "dtype": str(dtype), "kind": column_kind(dtype)}
        for col, dtype in df.dtypes.items()
    ]


def _contains_mask(series: pd.Series, needle: str) -> pd.Series:
    return series.astype(str).str.contains(needle, case=False, na=False, regex=False)


def apply_query(
    df: pd.DataFrame,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
) -> pd.DataFrame:
    """Filter, search, and sort. Always returns a new frame; never mutates ``df``."""
    by_name = {str(col): col for col in df.columns}

    for name, needle in (filters or {}).items():
        col = by_name.get(name)
        if col is None or not needle:
            continue
        df = df[_contains_mask(df[col], needle)]

    if search:
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask |= _contains_mask(df[col], search)
        df = df[mask]

    sort_col = by_name.get(sort_by) if sort_by else None
    if sort_col is not None:
        # astype(str) key on object columns sidesteps mixed-dtype TypeError;
        # order is lexicographic on those columns.
        key = (lambda s: s.astype(str)) if pd.api.types.is_object_dtype(df[sort_col]) else None
        df = df.sort_values(
            sort_col,
            ascending=(sort_dir != "desc"),
            kind="stable",
            na_position="last",
            key=key,
        )

    return df


def compute_stats(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """Per-column stats: nulls/unique always; min/max (+mean for numbers) when typed."""
    stats: Dict[str, Dict[str, Any]] = {}
    for col in df.columns:
        series = df[col]
        kind = column_kind(series.dtype)
        entry: Dict[str, Any] = {"nulls": int(series.isna().sum())}
        try:
            entry["unique"] = int(series.nunique())
        except TypeError:  # unhashable cells (lists/dicts) in object columns
            entry["unique"] = None
        if kind in ("number", "date") and len(series) > 0:
            entry["min"] = to_json_safe(series.min())
            entry["max"] = to_json_safe(series.max())
            if kind == "number":
                entry["mean"] = to_json_safe(series.mean())
        stats[str(col)] = entry
    return stats


def build_table_page(
    df: pd.DataFrame,
    *,
    page: int = 1,
    page_size: int = 100,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    stats: Optional[Dict[str, Dict[str, Any]]] = None,
    truncated: bool = False,
) -> Dict[str, Any]:
    """Run the full query pipeline and serialize one page as a JSON-safe dict."""
    total_rows = len(df)
    queried = apply_query(df, filters=filters, search=search, sort_by=sort_by, sort_dir=sort_dir)
    filtered_rows = len(queried)

    total_pages = max(1, ceil(filtered_rows / page_size))
    page = min(max(1, page), total_pages)
    page_df = queried.iloc[(page - 1) * page_size : page * page_size]

    rows = [
        [to_json_safe(value) for value in row] for row in page_df.itertuples(index=False, name=None)
    ]

    return {
        "kind": "table",
        "columns": column_specs(df),
        "rows": rows,
        "total_rows": total_rows,
        "filtered_rows": filtered_rows,
        "page": page,
        "total_pages": total_pages,
        "truncated": truncated,
        "stats": stats,
    }


def build_adapter_page(
    adapter,
    *,
    session_id: str,
    page: int = 1,
    page_size: int = 100,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
    include_stats: bool = False,
) -> Dict[str, Any]:
    """Serve one page from a foreign-DB source by pushing the whole query
    (filter/search/sort/pagination) down through a ``TableAdapter``.

    Same response shape as ``build_table_page`` plus ``approx`` (True when a
    count timed out and an estimate was used). Never truncated — the grid pages
    over the full remote table.
    """
    from datasources.adapters import cached_count  # noqa: PLC0415 — avoid cycle at import

    info = adapter.cache_info
    with getattr(adapter, "bound_session", nullcontext)():
        total = cached_count(
            (session_id, *info.key, None, None), info.ttl, lambda: adapter.count(None, None)
        )
        if filters or search:
            filters_key = json.dumps(filters or {}, sort_keys=True)
            filtered = cached_count(
                (session_id, *info.key, filters_key, search or None),
                info.ttl,
                lambda: adapter.count(filters, search),
            )
        else:
            filtered = total

        total_pages = max(1, ceil(filtered.value / page_size))
        page = min(max(1, page), total_pages)
        raw_rows = adapter.fetch_page(
            offset=(page - 1) * page_size,
            limit=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filters=filters,
            search=search,
        )
        columns = adapter.schema()
        stats = None
        if include_stats and adapter.supports_stats:
            stats = adapter.stats()

    from agents.sql_agent import working_copies as wc  # noqa: PLC0415 — avoid a cycle

    # Stych-stamped columns exist to align working copies; they are not the
    # user's data and must not reach the grid.
    keep = [i for i, c in enumerate(columns) if not wc.is_internal_column(c.name)]
    columns = [columns[i] for i in keep]
    raw_rows = [[row[i] for i in keep] for row in raw_rows]

    return {
        "kind": "table",
        "columns": [{"name": c.name, "dtype": c.db_type, "kind": c.kind} for c in columns],
        "rows": [[to_json_safe(v) for v in row] for row in raw_rows],
        "total_rows": total.value,
        "filtered_rows": filtered.value,
        "page": page,
        "total_pages": total_pages,
        "truncated": False,
        "approx": total.approx or filtered.approx,
        "stats": stats,
    }

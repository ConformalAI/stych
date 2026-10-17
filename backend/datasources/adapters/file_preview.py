"""DuckDB TableAdapter over a workspace parquet/csv file.

Pages with LIMIT/OFFSET against a lazy Arrow dataset — never materializes the
file into pandas or a DuckDB table. Filter/search SQL is shared with Postgres.
"""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd
import pyarrow.csv as pacsv
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from execution.duckdb import (
    DuckDBExecutionError,
    DuckDBKernel,
    DuckDBQueryTimeout,
    DuckDBRelation,
    DuckDBUnauthorizedPath,
)
from execution.duckdb.naming import quote_identifier

from .base import (
    PREVIEW_TIMEOUT_MS,
    SAMPLE_ROWS,
    AdapterColumn,
    AdapterError,
    AdapterTimeout,
    CountResult,
    SourceCacheInfo,
    TableAdapter,
    sql_type_kind,
)
from .preview_sql import build_count_sql, build_select_sql, quote_ident

_NAMED_PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
_RELATION = "preview"
_STATS_MAX_COLUMNS = 50


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso) and not isinstance(value, str):
        try:
            return iso()
        except Exception:
            pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return value


def _quote_relation(name: str) -> str:
    return quote_identifier(name)


def build_approx_stats_sql(source_ref: str, columns: List[AdapterColumn]) -> str:
    """One aggregate pass; unique counts use HyperLogLog, not COUNT(DISTINCT)."""
    exprs: List[str] = []
    for i, col in enumerate(columns[:_STATS_MAX_COLUMNS]):
        q = quote_ident(col.name)
        exprs.append(f"COUNT(*) - COUNT({q}) AS n{i}")
        exprs.append(f"approx_count_distinct({q}) AS u{i}")
        if col.kind in ("number", "date"):
            exprs.append(f"MIN({q}) AS mn{i}")
            exprs.append(f"MAX({q}) AS mx{i}")
        if col.kind == "number":
            exprs.append(f"AVG({q}) AS av{i}")
    return f"SELECT {', '.join(exprs)} FROM {source_ref}"


def _register_tabular(kernel: DuckDBKernel, path: Path) -> DuckDBRelation:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "parquet":
        return kernel.register_parquet(_RELATION, path)
    if suffix in {"csv", "tsv"}:
        delimiter = "\t" if suffix == "tsv" else ","
        fmt = pads.CsvFileFormat(parse_options=pacsv.ParseOptions(delimiter=delimiter))
        dataset = pads.dataset(str(path), format=fmt)
        return kernel.register_view(_RELATION, dataset, source=str(path))
    raise AdapterError(f"DuckDB preview does not support .{suffix} files.")


def _parquet_row_count(path: Path) -> Optional[int]:
    if path.suffix.lower() != ".parquet":
        return None
    return int(pq.ParquetFile(path).metadata.num_rows)


class FilePreviewAdapter(TableAdapter):
    dialect = "duckdb"
    supports_stats = True
    is_table = True

    def __init__(self, kernel: DuckDBKernel, relation: DuckDBRelation, path: Path) -> None:
        self.kernel = kernel
        self.relation = relation
        self.path = path
        self.source_ref = _quote_relation(relation.name)
        self.cache_info = SourceCacheInfo(
            key=("file", str(path), path.stat().st_mtime_ns),
            ttl=60.0,
        )
        self._columns = [
            AdapterColumn(
                name=column.name,
                db_type=column.data_type,
                kind=sql_type_kind(column.data_type),
            )
            for column in relation.columns
        ]

    def schema(self) -> List[AdapterColumn]:
        return list(self._columns)

    def _run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        max_rows: int = 1,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> Tuple[List[str], List[tuple]]:
        query = _NAMED_PARAM.sub(r"$\1", sql)
        try:
            result = self.kernel.query(
                query,
                params,
                max_rows=max(1, max_rows),
                timeout_seconds=max(timeout_ms, 1) / 1000,
            )
        except DuckDBQueryTimeout as exc:
            raise AdapterTimeout("Query timed out.") from exc
        except DuckDBExecutionError as exc:
            raise AdapterError(str(exc)) from exc
        frame = result.to_pandas()
        return list(frame.columns), list(frame.itertuples(index=False, name=None))

    def count(
        self, filters: Optional[Dict[str, str]] = None, search: Optional[str] = None
    ) -> CountResult:
        if not filters and not search:
            footer = _parquet_row_count(self.path)
            if footer is not None:
                return CountResult(footer)
        names = [c.name for c in self.schema()]
        sql, params = build_count_sql(self.source_ref, names, filters, search)
        try:
            _, rows = self._run(sql, params, max_rows=1)
        except AdapterTimeout:
            footer = _parquet_row_count(self.path) if not filters and not search else None
            if footer is not None:
                return CountResult(footer, approx=True)
            raise
        return CountResult(int(rows[0][0]) if rows else 0)

    def fetch_page(
        self,
        *,
        offset: int,
        limit: int,
        sort_by: Optional[str] = None,
        sort_dir: str = "asc",
        filters: Optional[Dict[str, str]] = None,
        search: Optional[str] = None,
    ) -> List[List[Any]]:
        names = [c.name for c in self.schema()]
        sql, params = build_select_sql(
            self.source_ref,
            names,
            offset=offset,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filters=filters,
            search=search,
        )
        _keys, rows = self._run(sql, params, max_rows=limit)
        return [list(row) for row in rows]

    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = SAMPLE_ROWS
    ) -> pd.DataFrame:
        ordered = [column.name for column in self.schema()]
        known = set(ordered)
        selected = [name for name in (columns or ordered) if name in known]
        projection = ", ".join(quote_ident(name) for name in selected) if selected else "*"
        keys, rows = self._run(
            f"SELECT {projection} FROM {self.source_ref} LIMIT :limit",
            {"limit": int(limit)},
            max_rows=limit,
        )
        return pd.DataFrame(rows, columns=keys)

    def stats(self) -> Optional[Dict[str, Dict[str, Any]]]:
        columns = self.schema()[:_STATS_MAX_COLUMNS]
        if not columns:
            return None
        try:
            keys, rows = self._run(build_approx_stats_sql(self.source_ref, columns), max_rows=1)
        except AdapterTimeout:
            return None
        if not rows:
            return None
        by_alias = dict(zip(keys, rows[0]))
        stats: Dict[str, Dict[str, Any]] = {}
        for i, col in enumerate(columns):
            unique = by_alias.get(f"u{i}")
            entry: Dict[str, Any] = {
                "nulls": int(by_alias.get(f"n{i}") or 0),
                "unique": int(unique) if unique is not None else None,
            }
            if col.kind in ("number", "date"):
                entry["min"] = _json_value(by_alias.get(f"mn{i}"))
                entry["max"] = _json_value(by_alias.get(f"mx{i}"))
            if col.kind == "number":
                avg = by_alias.get(f"av{i}")
                entry["mean"] = float(avg) if avg is not None else None
            stats[col.name] = entry
        return stats


@contextmanager
def open_file_preview_adapter(
    session_id: str,
    path: Path,
    *,
    allowed_root: Path | None = None,
) -> Iterator[FilePreviewAdapter]:
    """Register ``path`` as a lazy DuckDB view and yield a preview adapter.

    The kernel is closed when the context exits. ``allowed_root`` is the
    workspace directory the file must live under.
    """
    resolved = path.resolve()
    roots = [Path(allowed_root).resolve()] if allowed_root is not None else [resolved.parent]
    tmp = tempfile.TemporaryDirectory(prefix="stych-file-preview-")
    kernel = DuckDBKernel(session_id, Path(tmp.name) / "duckdb_tmp", allowed_data_roots=roots)
    try:
        relation = _register_tabular(kernel, resolved)
        yield FilePreviewAdapter(kernel, relation, resolved)
    except DuckDBQueryTimeout as exc:
        raise AdapterTimeout("Query timed out.") from exc
    except (DuckDBUnauthorizedPath, DuckDBExecutionError) as exc:
        raise AdapterError(str(exc)) from exc
    finally:
        kernel.close()
        tmp.cleanup()

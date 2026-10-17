"""TableAdapter facade over a session-scoped DuckDB snapshot relation."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from execution.session_plane import ResolvedDataPlane

from .base import AdapterColumn, CountResult, SourceCacheInfo, TableAdapter, sql_type_kind

_NAMED_PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class DuckDBTableAdapter(TableAdapter):
    dialect = "duckdb"
    supports_stats = False
    is_table = True

    def __init__(self, plane: ResolvedDataPlane) -> None:
        if plane.kernel is None or plane.relation is None:
            raise ValueError("DuckDBTableAdapter requires a materialized DuckDB plane")
        self.plane = plane
        self.kernel = plane.kernel
        self.source_ref = _quote(plane.relation_name)
        self.cache_info = SourceCacheInfo(
            key=("duckdb", plane.source_id, plane.snapshot_token),
            ttl=300.0,
        )

    def schema(self) -> List[AdapterColumn]:
        return [
            AdapterColumn(
                name=column.name,
                db_type=column.data_type,
                kind=sql_type_kind(column.data_type),
            )
            for column in self.plane.relation.columns
        ]

    def run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = 8_000,
    ) -> Tuple[List[str], List[tuple]]:
        query = _NAMED_PARAM.sub(r"$\1", sql)
        result = self.kernel.query(
            query,
            params,
            max_rows=max(1_000, min(self.plane.row_count + 1, 10_000)),
            timeout_seconds=max(timeout_ms, 1) / 1000,
        )
        frame = result.to_pandas()
        return list(frame.columns), list(frame.itertuples(index=False, name=None))

    def run_df(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = 8_000,
    ) -> pd.DataFrame:
        keys, rows = self.run(sql, params, timeout_ms)
        return pd.DataFrame(rows, columns=keys)

    def count(
        self,
        filters: Optional[Dict[str, str]] = None,
        search: Optional[str] = None,
    ) -> CountResult:
        if filters or search:
            # Preview pushdown remains owned by the source adapter. Persistent
            # FilterSpec queries build their own predicates through sql_service.
            raise ValueError("DuckDB snapshot count does not accept preview filters")
        _keys, rows = self.run(f"SELECT COUNT(*) FROM {self.source_ref}")
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
        if filters or search:
            raise ValueError("Use a FilterSpec for DuckDB snapshot filtering")
        order = ""
        names = {column.name for column in self.schema()}
        if sort_by and sort_by in names:
            direction = "DESC" if sort_dir == "desc" else "ASC"
            order = f" ORDER BY {_quote(sort_by)} {direction} NULLS LAST"
        sql = f"SELECT * FROM {self.source_ref}{order} LIMIT :limit OFFSET :offset"
        _keys, rows = self.run(sql, {"limit": int(limit), "offset": int(offset)})
        return [list(row) for row in rows]

    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = 10_000
    ) -> pd.DataFrame:
        ordered = [column.name for column in self.schema()]
        known = set(ordered)
        selected = [name for name in (columns or ordered) if name in known]
        projection = ", ".join(_quote(name) for name in selected) if selected else "*"
        return self.run_df(
            f"SELECT {projection} FROM {self.source_ref} LIMIT :limit", {"limit": limit}
        )

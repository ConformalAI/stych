"""
SQL Server table adapter: full-table preview pushdown (paging via
OFFSET-FETCH). Preview-only scope — no temp-table/diff path for SQL Server.

Keeps its own small engine pool (the SQL agent pool is Postgres-only); the
per-query timeout rides on pymssql's ``timeout`` connect arg.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements

from .base import (
    EXPORT_BATCH_ROWS,
    PREVIEW_TIMEOUT_MS,
    SAMPLE_ROWS,
    AdapterColumn,
    AdapterError,
    AdapterTimeout,
    CountResult,
    SourceCacheInfo,
    TableAdapter,
    cached_schema,
    escape_like,
    sql_type_kind,
)

logger = logging.getLogger(__name__)

_ENGINE_POOL: Dict[str, Engine] = {}
_POOL_LOCK = threading.Lock()


def _get_shared_engine(url: str) -> Engine:
    if url in _ENGINE_POOL:
        return _ENGINE_POOL[url]
    with _POOL_LOCK:
        if url not in _ENGINE_POOL:
            _ENGINE_POOL[url] = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=5,
                connect_args={
                    "login_timeout": 10,
                    # pymssql per-query timeout (seconds) — the preview guard.
                    "timeout": max(1, PREVIEW_TIMEOUT_MS // 1000),
                },
            )
        return _ENGINE_POOL[url]


def quote_ident(name: str) -> str:
    """Bracket-quote an identifier, doubling embedded ``]``."""
    return "[" + str(name).replace("]", "]]") + "]"


def validate_custom_query(query: str) -> str:
    try:
        statements = parse_read_only_statements(query, allowed_starts=frozenset({"SELECT", "WITH"}))
    except ReadOnlySQLError as exc:
        raise AdapterError("custom_query must be a SELECT statement") from exc
    if len(statements) != 1:
        raise AdapterError("custom_query must be a single statement")
    return statements[0]


def build_source_ref(
    schema: Optional[str], table: Optional[str], custom_query: Optional[str]
) -> str:
    if custom_query:
        return f"({validate_custom_query(custom_query)}) AS _src"
    if not table:
        raise AdapterError("Either table or custom_query is required")
    return f"{quote_ident(schema or 'dbo')}.{quote_ident(table)}"


def build_where(
    column_names: List[str],
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """T-SQL LIKE also treats ``[`` as a metacharacter — escape it too."""

    def _clause(col: str, param: str) -> str:
        return (
            f"LOWER(CAST({quote_ident(col)} AS NVARCHAR(MAX))) " f"LIKE LOWER(:{param}) ESCAPE '\\'"
        )

    known = set(column_names)
    clauses: List[str] = []
    params: Dict[str, str] = {}
    for i, (name, needle) in enumerate((filters or {}).items()):
        if name not in known or not needle:
            continue
        params[f"f{i}"] = f"%{escape_like(needle, extra='[')}%"
        clauses.append(_clause(name, f"f{i}"))
    if search:
        params["srch"] = f"%{escape_like(search, extra='[')}%"
        ors = " OR ".join(_clause(c, "srch") for c in column_names)
        if ors:
            clauses.append(f"({ors})")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def build_order_by(column_names: List[str], sort_by: Optional[str], sort_dir: str) -> str:
    """OFFSET-FETCH requires an ORDER BY; NULLS LAST needs a CASE (no native
    syntax in T-SQL). ``(SELECT NULL)`` is the standard no-preference order."""
    if sort_by and sort_by in set(column_names):
        q = quote_ident(sort_by)
        direction = "DESC" if sort_dir == "desc" else "ASC"
        return f" ORDER BY CASE WHEN {q} IS NULL THEN 1 ELSE 0 END, {q} {direction}"
    return " ORDER BY (SELECT NULL)"


def build_select_sql(
    source_ref: str,
    column_names: List[str],
    *,
    offset: int,
    limit: int,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    select_list = ", ".join(quote_ident(c) for c in column_names)
    where_sql, params = build_where(column_names, filters, search)
    order_sql = build_order_by(column_names, sort_by, sort_dir)
    params.update({"limit": int(limit), "offset": int(offset)})
    sql = (
        f"SELECT {select_list} FROM {source_ref}{where_sql}{order_sql} "
        f"OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY"
    )
    return sql, params


def build_count_sql(
    source_ref: str,
    column_names: List[str],
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    where_sql, params = build_where(column_names, filters, search)
    return f"SELECT COUNT_BIG(*) FROM {source_ref}{where_sql}", params


def _is_timeout(exc: DBAPIError) -> bool:
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()


class SqlServerTableAdapter(TableAdapter):
    dialect = "sqlserver"
    ident_quote = "["
    supports_stats = False

    def __init__(
        self,
        connection_url: str,
        *,
        schema: Optional[str] = None,
        table: Optional[str] = None,
        custom_query: Optional[str] = None,
        sample_kinds: Optional[Dict[str, str]] = None,
        cache_info: Optional[SourceCacheInfo] = None,
    ):
        self.connection_url = connection_url
        self.db_schema = schema or "dbo"
        self.table = table
        self.custom_query = custom_query
        self.source_ref = build_source_ref(self.db_schema, table, custom_query)
        self.is_table = custom_query is None
        # For custom queries INFORMATION_SCHEMA has no row; kinds fall back to
        # what the connect-time sample inferred.
        self.sample_kinds = sample_kinds or {}
        self.cache_info = cache_info or SourceCacheInfo(
            key=("mssql", connection_url, self.source_ref)
        )
        self._bound_conn = None

    def _run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> Tuple[List[str], List[tuple]]:
        del timeout_ms  # the pooled pymssql connection carries the query timeout
        try:
            if self._bound_conn is not None:
                result = self._bound_conn.execute(text(sql), params or {})
                return list(result.keys()), result.fetchall()
            engine = _get_shared_engine(self.connection_url)
            with engine.connect() as conn:
                result = conn.execute(text(sql), params or {})
                return list(result.keys()), result.fetchall()
        except DBAPIError as exc:
            if _is_timeout(exc):
                raise AdapterTimeout("Query timed out.") from exc
            raise AdapterError(str(getattr(exc, "orig", exc))) from exc

    def run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> Tuple[List[str], List[tuple]]:
        """Run one bounded read query for shared services such as SQL diff."""
        return self._run(sql, params, timeout_ms)

    def run_df(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> pd.DataFrame:
        keys, rows = self._run(sql, params, timeout_ms)
        return pd.DataFrame.from_records(rows, columns=keys, coerce_float=True)

    def stream_batches(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Full scan through one server-side cursor.

        One statement in one transaction sees a single snapshot. T-SQL caps rows
        with a ``TOP`` prefix rather than a ``LIMIT`` suffix, so the cap comes
        from the dialect instead of being appended.
        """
        from datasources.sql_dialect import get_dialect  # noqa: PLC0415

        dialect = get_dialect("sqlserver")
        names = [c.name for c in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        select_list = ", ".join(dialect.quote(name) for name in keep)
        statement = f"SELECT {dialect.select_prefix(max_rows)}{select_list} FROM {self.source_ref}"
        try:
            engine = _get_shared_engine(self.connection_url)
            with engine.begin() as conn:
                streaming = conn.execution_options(
                    stream_results=True, max_row_buffer=int(batch_rows)
                )
                result = streaming.execute(text(statement))
                for partition in result.partitions(int(batch_rows)):
                    yield pd.DataFrame(partition, columns=keep)
        except DBAPIError as exc:
            if _is_timeout(exc):
                raise AdapterTimeout("Export timed out.") from exc
            raise AdapterError(str(getattr(exc, "orig", exc))) from exc

    @contextmanager
    def bound_session(self) -> Iterator[None]:
        if self._bound_conn is not None:
            yield
            return
        engine = _get_shared_engine(self.connection_url)
        with engine.connect() as conn:
            self._bound_conn = conn
            try:
                yield
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._bound_conn = None

    def schema(self) -> List[AdapterColumn]:
        return cached_schema(
            ("schema", *self.cache_info.key), self.cache_info.ttl, self._load_schema
        )

    def _load_schema(self) -> List[AdapterColumn]:
        if self.is_table:
            _, rows = self._run(
                """
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :tname
                ORDER BY ORDINAL_POSITION
                """,
                {"schema": self.db_schema, "tname": self.table},
            )
            if not rows:
                raise AdapterError(f"Table {self.db_schema}.{self.table} was not found.")
            return [AdapterColumn(name=r[0], db_type=r[1], kind=sql_type_kind(r[1])) for r in rows]
        keys, _ = self._run(f"SELECT TOP 0 * FROM {self.source_ref}")
        return [
            AdapterColumn(name=k, db_type="", kind=self.sample_kinds.get(k, "string")) for k in keys
        ]

    def count(
        self, filters: Optional[Dict[str, str]] = None, search: Optional[str] = None
    ) -> CountResult:
        names = [c.name for c in self.schema()]
        sql, params = build_count_sql(self.source_ref, names, filters, search)
        try:
            _, rows = self._run(sql, params)
            return CountResult(int(rows[0][0]))
        except AdapterTimeout:
            if not filters and not search and self.is_table:
                _, rows = self._run(
                    """
                    SELECT SUM(row_count) FROM sys.dm_db_partition_stats
                    WHERE object_id = OBJECT_ID(:qname) AND index_id IN (0, 1)
                    """,
                    {"qname": f"{quote_ident(self.db_schema)}.{quote_ident(self.table)}"},
                )
                if rows and rows[0][0] is not None:
                    return CountResult(int(rows[0][0]), approx=True)
            raise

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
        _, rows = self._run(sql, params)
        return [list(r) for r in rows]

    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = SAMPLE_ROWS
    ) -> pd.DataFrame:
        names = [c.name for c in self.schema()]
        if columns:
            names = [n for n in names if n in set(columns)]
        select_list = ", ".join(quote_ident(c) for c in names)
        keys, rows = self._run(f"SELECT TOP {int(limit)} {select_list} FROM {self.source_ref}")
        return pd.DataFrame(rows, columns=keys)

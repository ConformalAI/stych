"""
PostgreSQL table adapter: full-table preview pushdown over the user's database.

Reuses the SQL agent's shared engine pool (same connections, same pool), but
every preview query runs inside a transaction with ``SET LOCAL
statement_timeout`` so the short interactive timeout never leaks onto pooled
connections used by the agent (which keeps its 5-minute default).
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements

from .base import (
    EXPORT_BATCH_ROWS,
    EXPORT_TIMEOUT_MS,
    PREVIEW_TIMEOUT_MS,
    SAMPLE_ROWS,
    AdapterColumn,
    AdapterError,
    AdapterTimeout,
    CountResult,
    SourceCacheInfo,
    TableAdapter,
    cached_schema,
    sql_type_kind,
)
from .preview_sql import (
    build_count_sql,
    build_select_sql,
    build_where,
    quote_ident,
)

logger = logging.getLogger(__name__)

_STATS_MAX_COLUMNS = 50

# psycopg2 cursor.description type OIDs → readable type name, for custom-query
# sources where information_schema has no row.
_OID_TYPES = {
    16: "boolean",
    20: "bigint",
    21: "smallint",
    23: "integer",
    25: "text",
    114: "json",
    700: "real",
    701: "double precision",
    1042: "character",
    1043: "character varying",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamptz",
    1700: "numeric",
    2950: "uuid",
    3802: "jsonb",
}


def validate_custom_query(query: str) -> str:
    """SELECT/WITH-only, single statement — same rule as the SQL agent runner."""
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
    """FROM-clause reference: a quoted table or a wrapped, validated subquery."""
    if custom_query:
        return f"({validate_custom_query(custom_query)}) AS _src"
    if not table:
        raise AdapterError("Either table or custom_query is required")
    return f"{quote_ident(schema or 'public')}.{quote_ident(table)}"


def build_stats_sql(source_ref: str, columns: List[AdapterColumn]) -> str:
    """One aggregate pass: nulls/unique always, min/max for number/date, avg for
    numbers. Aliases are positional so odd column names can't break parsing."""
    exprs: List[str] = []
    for i, col in enumerate(columns[:_STATS_MAX_COLUMNS]):
        q = quote_ident(col.name)
        exprs.append(f"COUNT(*) - COUNT({q}) AS n{i}")
        exprs.append(f"COUNT(DISTINCT {q}) AS u{i}")
        if col.kind in ("number", "date"):
            exprs.append(f"MIN({q}) AS mn{i}")
            exprs.append(f"MAX({q}) AS mx{i}")
        if col.kind == "number":
            exprs.append(f"AVG({q}) AS av{i}")
    return f"SELECT {', '.join(exprs)} FROM {source_ref}"


def _is_statement_timeout(exc: DBAPIError) -> bool:
    if getattr(getattr(exc, "orig", None), "pgcode", None) == "57014":
        return True
    return "statement timeout" in str(exc).lower()


class PostgresTableAdapter(TableAdapter):
    dialect = "postgres"
    supports_stats = True

    def __init__(
        self,
        connection_url: str,
        *,
        schema: Optional[str] = None,
        table: Optional[str] = None,
        custom_query: Optional[str] = None,
        cache_info: Optional[SourceCacheInfo] = None,
    ):
        self.connection_url = connection_url
        self.db_schema = schema or "public"
        self.table = table
        self.custom_query = custom_query
        self.source_ref = build_source_ref(self.db_schema, table, custom_query)
        self.is_table = custom_query is None
        self.cache_info = cache_info or SourceCacheInfo(key=("pg", connection_url, self.source_ref))
        self._bound_conn = None

    # -- execution ----------------------------------------------------------

    def _engine(self):
        # Deferred: importing the runner at module scope drags in the whole
        # agents package (and its DB requirement), which would make the pure
        # SQL builders in this module untestable without an environment.
        from agents.sql_agent.runner import get_shared_engine  # noqa: PLC0415

        return get_shared_engine(self.connection_url)

    @contextmanager
    def bound_session(self) -> Iterator[None]:
        if self._bound_conn is not None:
            yield
            return
        with self._engine().connect() as conn:
            try:
                conn.execute(text(f"SET LOCAL statement_timeout = {PREVIEW_TIMEOUT_MS}"))
                self._bound_conn = conn
                yield
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._bound_conn = None

    def _run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> Tuple[List[str], List[tuple], Any]:
        try:
            if self._bound_conn is not None:
                result = self._bound_conn.execute(text(sql), params or {})
                keys = list(result.keys())
                rows = result.fetchall()
                description = getattr(result.cursor, "description", None)
                return keys, rows, description
            with self._engine().begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
                result = conn.execute(text(sql), params or {})
                keys = list(result.keys())
                rows = result.fetchall()
                description = getattr(result.cursor, "description", None)
                return keys, rows, description
        except DBAPIError as exc:
            if _is_statement_timeout(exc):
                raise AdapterTimeout("Query timed out.") from exc
            raise AdapterError(str(getattr(exc, "orig", exc))) from exc

    def run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> Tuple[List[str], List[tuple]]:
        """Run one read query under the preview timeout (used by the SQL diff)."""
        keys, rows, _ = self._run(sql, params, timeout_ms)
        return keys, rows

    def run_df(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> pd.DataFrame:
        """Like ``run`` but through ``pd.read_sql`` so numeric types land as
        floats/ints (Decimal → float), which the diff classifiers expect."""
        engine = self._engine()
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
                return pd.read_sql(text(sql), conn, params=params or {}, coerce_float=True)
        except DBAPIError as exc:
            if _is_statement_timeout(exc):
                raise AdapterTimeout("Query timed out.") from exc
            raise AdapterError(str(getattr(exc, "orig", exc))) from exc

    # -- interface ----------------------------------------------------------

    def schema(self) -> List[AdapterColumn]:
        return cached_schema(
            ("schema", *self.cache_info.key), self.cache_info.ttl, self._load_schema
        )

    def _load_schema(self) -> List[AdapterColumn]:
        if self.is_table:
            keys_rows = self._run(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = :tname
                ORDER BY ordinal_position
                LIMIT 500
                """,
                {"schema": self.db_schema, "tname": self.table},
            )
            cols = [
                AdapterColumn(name=r[0], db_type=r[1], kind=sql_type_kind(r[1]))
                for r in keys_rows[1]
            ]
            if cols:
                return cols
            raise AdapterError(f"Table {self.db_schema}.{self.table} was not found.")
        # Custom query: LIMIT 0 round trip; type names come from cursor OIDs.
        keys, _rows, description = self._run(f"SELECT * FROM {self.source_ref} LIMIT 0")
        oids = [d[1] for d in description] if description else [None] * len(keys)
        return [
            AdapterColumn(
                name=k,
                db_type=_OID_TYPES.get(oid, "unknown"),
                kind=sql_type_kind(_OID_TYPES.get(oid, "")),
            )
            for k, oid in zip(keys, oids)
        ]

    def count(
        self, filters: Optional[Dict[str, str]] = None, search: Optional[str] = None
    ) -> CountResult:
        names = [c.name for c in self.schema()]
        sql, params = build_count_sql(self.source_ref, names, filters, search)
        try:
            _, rows, _ = self._run(sql, params)
            return CountResult(int(rows[0][0]))
        except AdapterTimeout:
            return self._estimate_count(names, filters, search)

    def _estimate_count(
        self, names: List[str], filters: Optional[Dict[str, str]], search: Optional[str]
    ) -> CountResult:
        if not filters and not search and self.is_table:
            _, rows, _ = self._run(
                "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(:qname)",
                {"qname": f"{quote_ident(self.db_schema)}.{quote_ident(self.table)}"},
            )
            if rows and rows[0][0] is not None and int(rows[0][0]) >= 0:
                return CountResult(int(rows[0][0]), approx=True)
            raise AdapterTimeout("Count timed out and no estimate is available.")
        where_sql, params = build_where(names, filters, search)
        _, rows, _ = self._run(
            f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {self.source_ref}{where_sql}", params
        )
        try:
            payload = rows[0][0]
            plan = json.loads(payload) if isinstance(payload, str) else payload
            return CountResult(int(plan[0]["Plan"]["Plan Rows"]), approx=True)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AdapterTimeout("Count timed out and no estimate is available.") from exc

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
        # ctid keeps unsorted pagination stable across requests on real tables;
        # subqueries have no ctid — their order is whatever the plan yields.
        tiebreak = "ctid" if self.is_table else None
        sql, params = build_select_sql(
            self.source_ref,
            names,
            offset=offset,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filters=filters,
            search=search,
            stable_tiebreak=tiebreak,
        )
        _, rows, _ = self._run(sql, params)
        return [list(r) for r in rows]

    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = SAMPLE_ROWS
    ) -> pd.DataFrame:
        names = [c.name for c in self.schema()]
        if columns:
            names = [n for n in names if n in set(columns)]
        select_list = ", ".join(quote_ident(c) for c in names)
        keys, rows, _ = self._run(
            f"SELECT {select_list} FROM {self.source_ref} LIMIT :n", {"n": int(limit)}
        )
        return pd.DataFrame(rows, columns=keys)

    def stream_batches(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Full scan through one server-side cursor.

        A single statement inside one transaction sees one MVCC snapshot, so the
        export is internally consistent even while the table is written. Rows are
        buffered a batch at a time rather than materialized whole.
        """
        names = [c.name for c in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        select_list = ", ".join(quote_ident(c) for c in keep)
        sql = f"SELECT {select_list} FROM {self.source_ref}"
        if max_rows is not None:
            sql += f" LIMIT {int(max_rows)}"
        try:
            with self._engine().begin() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = {EXPORT_TIMEOUT_MS}"))
                streaming = conn.execution_options(
                    stream_results=True, max_row_buffer=int(batch_rows)
                )
                result = streaming.execute(text(sql))
                for partition in result.partitions(int(batch_rows)):
                    yield pd.DataFrame(partition, columns=keep)
        except DBAPIError as exc:
            if _is_statement_timeout(exc):
                raise AdapterTimeout("Export timed out.") from exc
            raise AdapterError(str(getattr(exc, "orig", exc))) from exc

    def stats(self) -> Optional[Dict[str, Dict[str, Any]]]:
        columns = self.schema()[:_STATS_MAX_COLUMNS]
        if not columns:
            return None
        try:
            keys, rows, _ = self._run(build_stats_sql(self.source_ref, columns))
        except AdapterTimeout:
            return None
        if not rows:
            return None
        by_alias = dict(zip(keys, rows[0]))
        stats: Dict[str, Dict[str, Any]] = {}
        for i, col in enumerate(columns):
            entry: Dict[str, Any] = {
                "nulls": int(by_alias.get(f"n{i}") or 0),
                "unique": int(by_alias[f"u{i}"]) if by_alias.get(f"u{i}") is not None else None,
            }
            if col.kind in ("number", "date"):
                entry["min"] = by_alias.get(f"mn{i}")
                entry["max"] = by_alias.get(f"mx{i}")
            if col.kind == "number":
                avg = by_alias.get(f"av{i}")
                entry["mean"] = float(avg) if avg is not None else None
            stats[col.name] = entry
        return stats

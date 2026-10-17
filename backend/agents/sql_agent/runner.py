"""
Safe SQL runner for Postgres: only operates on stych_temp schema.
Creates temp tables from user tables, runs cleaning/analysis SQL there.
"""

import hashlib
import logging
import re
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from agents.sql_agent.write_guard import WriteGuardError, validate_write_statements
from datasources.sql_dialect import get_dialect
from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements

logger = logging.getLogger(__name__)

TEMP_SCHEMA = "stych_temp"
# Only allow identifiers in this schema for writes
ALLOWED_WRITE_SCHEMA = TEMP_SCHEMA

# Stych-owned row number stamped onto every materialized copy. Warehouses expose
# no physical row address, so this is what lets two copies be aligned row-for-row;
# branches clone the column rather than recomputing it, so one lineage shares one
# identity even when the source has no natural key.
ROW_ID_COLUMN = "_stych_row_id"

# ---------------------------------------------------------------------------
# Module-level engine pool — one Engine (with its connection pool) per URL.
# Avoids the expensive create/dispose cycle that used to happen on every call.
# ---------------------------------------------------------------------------
_ENGINE_POOL: Dict[str, Engine] = {}
_POOL_LOCK = threading.Lock()


#: Per-driver connection arguments. These are driver keywords, not SQLAlchemy
#: ones, so they cannot be shared: ``connect_timeout`` and ``options`` are
#: libpq's, and pymssql rejects both outright.
_CONNECT_ARGS = {
    # connect_timeout: fail fast when the host is unreachable.
    # options: statement_timeout kills a runaway query (e.g. a full scan of
    # 700K rows) after 5 minutes instead of hanging the thread pool.
    "postgresql": {"connect_timeout": 10, "options": "-c statement_timeout=300000"},
    "mssql": {"login_timeout": 10, "timeout": 300},
}


def _connect_args(url: str) -> Dict[str, Any]:
    """Driver keywords for *url*, or none when the driver takes none we know of."""
    backend = str(url).split("://", 1)[0].split("+", 1)[0].lower()
    return dict(_CONNECT_ARGS.get(backend, {}))


def _get_shared_engine(url: str) -> Engine:
    """Return the cached Engine for *url*, creating it on first call."""
    if url in _ENGINE_POOL:
        return _ENGINE_POOL[url]
    with _POOL_LOCK:
        if url not in _ENGINE_POOL:
            _ENGINE_POOL[url] = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=5,
                connect_args=_connect_args(url),
            )
            logger.debug("Created shared SQLAlchemy engine for %s", url[:40])
        return _ENGINE_POOL[url]


# Public alias so other layers (e.g. datasources.adapters) share the same pool
# without importing a private name.
get_shared_engine = _get_shared_engine


def evict_engine(url: str) -> None:
    """
    Dispose and evict the shared engine for *url*.
    Call this when a session is permanently deleted so connections are released.
    """
    with _POOL_LOCK:
        engine = _ENGINE_POOL.pop(url, None)
    if engine:
        try:
            engine.dispose()
            logger.debug("Evicted engine for %s", url[:40])
        except Exception as exc:
            logger.warning("Error evicting engine: %s", exc)


class SQLRunnerError(Exception):
    """Raised when SQL runner operations fail."""


def _sanitize_identifier(name: str) -> str:
    """Return a safe table/column identifier (alphanumeric + underscore)."""
    if not name:
        raise ValueError("Empty identifier")
    safe = re.subn(r"[^a-zA-Z0-9_]", "_", name)[0]
    if not safe:
        raise ValueError(f"Invalid identifier: {name}")
    return safe[:63]  # Postgres identifier length limit


def _quote_ident(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def temp_table_prefix(session_id: str) -> str:
    """The physical-name prefix shared by every table stych creates for this
    session (``stych_<prefix><uuid>``). GC and session deletion scan by it."""
    return f"{_sanitize_identifier(session_id.replace('-', '_')[:32])}_"


def dataset_table_prefix(session_id: str, file_id: str) -> str:
    """Prefix scratch objects with both session and dataset ownership.

    A session can attach several relations that share one scratch schema.  A
    session-only prefix makes per-dataset orphan collection unsafe: one data
    context cannot distinguish its own tables from a sibling attachment's.
    The short digest is stable, identifier-safe, and keeps PostgreSQL names
    below its 63-byte limit even with the longest generated suffix.
    """
    owner = hashlib.sha256(str(file_id or "unknown").encode()).hexdigest()[:8]
    return f"stych_{temp_table_prefix(session_id)}{owner}_"


class PostgresSQLRunner:
    #: Statements run in one transaction and roll back together.
    atomic_writes = True

    """
    Runs SQL only against the stych_temp schema (and read-only against user tables).
    Used to duplicate source table into temp, then clean/analyze in place.

    Engines are shared via the module-level pool so that we never pay the
    create/dispose cost on every individual call.
    """

    def __init__(self, connection_url: str):
        if not connection_url or not connection_url.strip():
            raise SQLRunnerError("connection_url is required")
        url = connection_url.strip()
        # Guard against a redacted/placeholder URL from a restored snapshot ever
        # reaching create_engine (which would raise an opaque "Could not parse URL").
        if url == "[REDACTED]" or "://" not in url:
            raise SQLRunnerError(
                "Database connection is unavailable for this session — please reconnect the table."
            )
        self.connection_url = url

    @property
    def temp_schema(self) -> str:
        return TEMP_SCHEMA

    @property
    def credential_key(self) -> str:
        return self.connection_url

    def quote_relation(self, schema: str, table: str) -> str:
        return _quote_ident(schema, table)

    def _get_engine(self) -> Engine:
        return _get_shared_engine(self.connection_url)

    def _execute(self, sql: str, params: Optional[dict] = None) -> None:
        """Execute DDL/DML (no return)."""
        with self._get_engine().connect() as conn:
            conn.execute(text(sql), params or {})
            conn.commit()

    def _execute_read(self, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
        """Execute SELECT and return list of dicts."""
        with self._get_engine().connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            result = conn.execute(text(sql), params or {})
            rows = result.fetchall()
            if not rows:
                return []
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in rows]

    def _execute_read_df(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
        """Execute SELECT and return a small DataFrame."""
        with self._get_engine().connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            return pd.read_sql(text(sql), conn, params=params or {}, coerce_float=True)

    def ensure_temp_schema(self) -> None:
        """Create stych_temp schema if it does not exist."""
        self._execute(f'CREATE SCHEMA IF NOT EXISTS "{TEMP_SCHEMA}"')

    def create_temp_table_from_source(
        self,
        session_id: str,
        source_schema: str,
        source_table: Optional[str],
        custom_query: Optional[str],
        max_rows: int = 0,
        table_name: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Create a temporary table as a copy of the source. Only reads from user schema.
        Returns (temp_schema, temp_table).
        """
        self.ensure_temp_schema()
        temp_table = _sanitize_identifier(table_name) if table_name else None
        if not temp_table:
            temp_table = f"stych_{temp_table_prefix(session_id)}{uuid.uuid4().hex[:8]}"

        if custom_query:
            try:
                statements = parse_read_only_statements(
                    custom_query, allowed_starts=frozenset({"SELECT", "WITH"})
                )
            except ReadOnlySQLError as exc:
                raise SQLRunnerError("custom_query must be a SELECT statement") from exc
            if len(statements) != 1:
                raise SQLRunnerError("custom_query must be a SELECT statement")
            source_sql = f"({statements[0]}) AS _src"
        else:
            if not source_table:
                raise SQLRunnerError("Either source_table or custom_query is required")
            q_schema = _sanitize_identifier(source_schema)
            q_table = _sanitize_identifier(source_table)
            source_sql = f'"{q_schema}"."{q_table}" AS _src'

        # Stamp a Stych-owned row number so copies can be aligned row-for-row
        # later. Branches clone this column rather than recomputing it, so a
        # whole lineage shares one identity even without a natural key.
        row_id = get_dialect("postgres").row_id_expr()
        limit_sql = f" LIMIT {max_rows}" if max_rows and max_rows > 0 else ""
        create_sql = (
            f'CREATE TABLE "{TEMP_SCHEMA}"."{temp_table}" AS '
            f'SELECT {row_id} AS "{ROW_ID_COLUMN}", _src.* '
            f"FROM {source_sql}{limit_sql}"
        )
        logger.info("Creating temp table from source: %s", temp_table)
        self._execute(create_sql)
        return (TEMP_SCHEMA, temp_table)

    def duplicate_temp_table(self, source_table: str, new_table: str) -> None:
        """Clone one stych_temp table into another (branch from a working copy)."""
        src = _sanitize_identifier(source_table)
        dst = _sanitize_identifier(new_table)
        self.ensure_temp_schema()
        self._execute(
            f'CREATE TABLE "{TEMP_SCHEMA}"."{dst}" AS SELECT * FROM "{TEMP_SCHEMA}"."{src}"'
        )

    def drop_temp_table(self, table: str) -> None:
        """Drop a table in the stych_temp schema only."""
        self._execute(f'DROP TABLE IF EXISTS "{TEMP_SCHEMA}"."{_sanitize_identifier(table)}"')

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        """Names of stych_temp tables starting with *prefix* (GC / delete scan)."""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._execute_read(
            r"""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = :schema AND table_name LIKE :pattern ESCAPE '\'
            LIMIT 200
            """,
            {"schema": TEMP_SCHEMA, "pattern": f"{escaped}%"},
        )
        return [r["table_name"] for r in rows]

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        """Return column name and data type for the table."""
        q = """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :tname
            ORDER BY ordinal_position
            LIMIT 500
        """
        rows = self._execute_read(
            q,
            {"schema": schema, "tname": table},
        )
        return [
            {
                "column_name": r["column_name"],
                "data_type": r["data_type"],
                "is_nullable": r["is_nullable"],
            }
            for r in rows
        ]

    def table_exists(self, schema: str, table: str) -> bool:
        q = """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema AND table_name = :tname
            LIMIT 1
        """
        rows = self._execute_read(q, {"schema": schema, "tname": table})
        return bool(rows)

    def create_or_replace_view(
        self, view_schema: str, view_name: str, target_schema: str, target_table: str
    ) -> None:
        """Create or replace a view pointing to the target table."""
        view_ident = _quote_ident(view_schema, view_name)
        target_ident = _quote_ident(target_schema, target_table)
        # Drop first to avoid "cannot drop columns from view" when column set changes.
        self._execute(f"DROP VIEW IF EXISTS {view_ident}")
        self._execute(f"CREATE VIEW {view_ident} AS SELECT * FROM {target_ident}")

    def execute_statements_transaction(
        self, statements: List[str], *, allowed_schema: str = TEMP_SCHEMA
    ) -> None:
        """Execute statements in a single transaction.

        Mutating statements must reference only *allowed_schema* (default: stych_temp).
        Pass the user's schema only after explicit cleaning approval.
        """
        if not statements:
            return
        self._validate_write_statements(statements, allowed_schema=allowed_schema)
        with self._get_engine().begin() as conn:
            for sql in statements:
                conn.execute(text(sql))

    @staticmethod
    def _validate_write_statements(statements: List[str], *, allowed_schema: str) -> None:
        """Confine writes to the scratch schema (see ``write_guard``)."""
        try:
            validate_write_statements(statements, allowed_schema=allowed_schema, fold="lower")
        except WriteGuardError as exc:
            raise SQLRunnerError(str(exc)) from exc

    def get_row_count(self, schema: str, table: str) -> int:
        """Return row count for the table."""
        quoted = _quote_ident(schema, table)
        rows = self._execute_read(f"SELECT COUNT(*) AS c FROM {quoted}")
        return int(rows[0]["c"]) if rows else 0

    def get_total_nulls(self, schema: str, table: str) -> int:
        """Return total NULL count across all columns for the table."""
        cols = self.get_table_columns(schema, table)
        if not cols:
            return 0
        quoted = _quote_ident(schema, table)
        clauses = " + ".join(
            f'COALESCE(SUM(CASE WHEN "{c["column_name"]}" IS NULL THEN 1 ELSE 0 END), 0)'
            for c in cols
        )
        rows = self._execute_read(f"SELECT {clauses} AS total_nulls FROM {quoted}")
        return int(rows[0]["total_nulls"]) if rows else 0

    def run_sql_read_only(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
        """Run one read-only query and return a DataFrame.

        The driver receives a single statement. Callers that want a batch should
        split with ``parse_read_only_statements`` and invoke this once per piece.
        """
        try:
            statements = parse_read_only_statements(sql)
        except ReadOnlySQLError as exc:
            raise SQLRunnerError(str(exc)) from exc
        if len(statements) != 1:
            raise SQLRunnerError("Multiple statements not allowed")
        return self._execute_read_df(statements[0], params)

    def run_sql_on_temp(
        self, schema: str, table: str, sql: str, params: Optional[dict] = None
    ) -> None:
        """
        Run DDL/DML that must reference only the given temp table (same schema).
        Validates that only stych_temp is referenced for writes.
        """
        if schema != TEMP_SCHEMA or not table:
            raise SQLRunnerError("run_sql_on_temp only allows stych_temp schema")
        # Allow only our temp table in the SQL (simple check: no other schema.table pattern for non-stych_temp)
        self._execute(sql, params)

    def delete_duplicates_keep_first(
        self,
        schema: str,
        table: str,
        subset_columns: Optional[List[str]] = None,
        sql_log: Optional[List[str]] = None,
    ) -> int:
        """
        Remove duplicate rows, keeping first occurrence. Uses a new table and swap.
        Returns number of rows removed.
        """
        if schema != TEMP_SCHEMA:
            raise SQLRunnerError("delete_duplicates_keep_first only on stych_temp")
        cols = self.get_table_columns(schema, table)
        col_names = [c["column_name"] for c in cols]
        subset = subset_columns or col_names
        subset_safe = [_sanitize_identifier(c) for c in subset if c in col_names]
        if not subset_safe:
            subset_safe = [f'"{c}"' for c in col_names]

        quoted = _quote_ident(schema, table)
        before = self.get_row_count(schema, table)
        new_name = f"{table}_dedup_{uuid.uuid4().hex[:6]}"
        # DISTINCT ON (cols) in Postgres requires ORDER BY same cols; keep first row per group
        order_expr = ", ".join(f'"{s}"' for s in subset_safe)
        create_sql = (
            f'CREATE TABLE "{schema}"."{new_name}" AS '
            f"SELECT * FROM ( SELECT DISTINCT ON ({order_expr}) * FROM {quoted} ORDER BY {order_expr} ) sub"
        )
        drop_sql = f"DROP TABLE {quoted}"
        rename_sql = f'ALTER TABLE "{schema}"."{new_name}" RENAME TO "{table}"'
        if sql_log is not None:
            sql_log.extend([create_sql, drop_sql, rename_sql])
        self._execute(create_sql)
        self._execute(drop_sql)
        self._execute(rename_sql)
        after = self.get_row_count(schema, table)
        return before - after

    def run_analysis_query(self, schema: str, table: str, select_sql: str) -> pd.DataFrame:
        """
        Run a single SELECT that reads from the given temp table.
        Table must be in stych_temp. Returns result as DataFrame.
        """
        if schema != TEMP_SCHEMA:
            raise SQLRunnerError("run_analysis_query only on stych_temp")
        return self.run_sql_read_only(select_sql)

    def dispose(self) -> None:
        """No-op — the engine is shared via the module-level pool.
        Call evict_engine(url) explicitly when a session is permanently deleted."""

"""Scratch-schema SQL runner for Databricks Unity Catalog.

Working copies live in ``{scratch_catalog}.{scratch_schema}`` (default
``{connected_catalog}.stych_temp``). Source tables are read-only; DML must
name the scratch schema.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pandas as pd

from agents.sql_agent.runner import (
    SQLRunnerError,
    _sanitize_identifier,
    temp_table_prefix,
)
from agents.sql_agent.warehouse_base import WRITE_TIMEOUT_SECONDS, WarehouseSQLRunner
from datasources.databricks import (
    DEFAULT_SCRATCH_SCHEMA,
    DatabricksLoadError,
    execute_databricks_statement,
    run_databricks_query,
    scratch_location,
)
from datasources.sql_dialect import get_dialect
from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements
from datasources.warehouse import quote_ident, relation_ref

logger = logging.getLogger(__name__)

# Without column mapping, Delta rejects column names containing a space or any
# of ,;{}()\n\t= — so a source table with "Transaction ID" cannot be cloned at
# all. Setting it on the scratch table we create avoids asking the customer to
# ALTER their own table. Column mapping requires this reader/writer protocol.
_COLUMN_MAPPING_TBLPROPERTIES = (
    "TBLPROPERTIES ("
    "'delta.columnMapping.mode' = 'name', "
    "'delta.minReaderVersion' = '2', "
    "'delta.minWriterVersion' = '5')"
)

_IDENT = "`"


def _qi(name: str) -> str:
    return quote_ident(str(name), _IDENT)


class DatabricksSQLRunner(WarehouseSQLRunner):
    """Clone source relations into a Unity Catalog scratch schema and run SQL there."""

    engine_label = "Databricks"
    row_count_alias = "c"

    def __init__(self, connection_config: Mapping[str, Any]):
        cfg = dict(connection_config)
        catalog, schema = scratch_location(cfg)
        if not catalog:
            raise SQLRunnerError(
                "Databricks catalog is required for the scratch working copy — reconnect the table."
            )
        token = str(cfg.get("access_token") or "").strip()
        if not token or token == "[REDACTED]":
            raise SQLRunnerError(
                "Databricks connection is unavailable for this session — please reconnect the table."
            )
        self.connection_config = cfg
        self.scratch_catalog = catalog
        self._temp_schema = schema or DEFAULT_SCRATCH_SCHEMA
        self.source_catalog = str(cfg.get("catalog") or catalog)
        self._dialect = get_dialect("databricks")

    @classmethod
    def _apply_meta_overrides(cls, meta: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
        out = dict(cfg)
        if meta.get("scratch_catalog"):
            out["scratch_catalog"] = meta["scratch_catalog"]
        if meta.get("scratch_schema"):
            out["scratch_schema"] = meta["scratch_schema"]
        return out

    # -- identity -----------------------------------------------------------

    @property
    def credential_key(self) -> str:
        host = str(self.connection_config.get("server_hostname") or "")
        return f"{host}|{self.scratch_catalog}|{self.temp_schema}"

    def quote_relation(self, schema: str, table: str) -> str:
        return relation_ref(database=self.scratch_catalog, schema=schema, table=table, quote=_IDENT)

    def _source_ref(self, source_schema: str, source_table: str) -> str:
        return relation_ref(
            database=self.source_catalog,
            schema=source_schema,
            table=source_table,
            quote=_IDENT,
        )

    # -- execution ----------------------------------------------------------

    def _execute(
        self, sql: str, *, timeout_seconds: int = WRITE_TIMEOUT_SECONDS, connection=None
    ) -> None:
        try:
            execute_databricks_statement(
                self.connection_config,
                sql,
                timeout_seconds=timeout_seconds,
                connection=connection,
            )
        except DatabricksLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc

    def _execute_read(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> List[Dict[str, Any]]:
        try:
            names, rows = run_databricks_query(
                self.connection_config, sql, parameters, timeout_seconds=timeout_seconds
            )
        except DatabricksLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return [dict(zip(names, row)) for row in rows]

    def _execute_read_df(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> pd.DataFrame:
        try:
            names, rows = run_databricks_query(
                self.connection_config, sql, parameters, timeout_seconds=timeout_seconds
            )
        except DatabricksLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return pd.DataFrame.from_records(rows, columns=names)

    def ensure_temp_schema(self, *, connection=None) -> None:
        self._execute(
            f"CREATE SCHEMA IF NOT EXISTS {_qi(self.scratch_catalog)}.{_qi(self.temp_schema)}",
            connection=connection,
        )

    def create_temp_table_from_source(
        self,
        session_id: str,
        source_schema: str,
        source_table: Optional[str],
        custom_query: Optional[str],
        max_rows: int = 1_000_000,
        table_name: Optional[str] = None,
    ) -> Tuple[str, str]:
        del max_rows  # full-table clone; Databricks SOURCE plane does not cap rows
        temp_table = _sanitize_identifier(table_name) if table_name else None
        if not temp_table:
            temp_table = f"stych_{temp_table_prefix(session_id)}{uuid.uuid4().hex[:8]}"
        dest = self.quote_relation(self.temp_schema, temp_table)
        from datasources.databricks import _connect  # noqa: PLC0415
        from datasources.dbapi import borrowed  # noqa: PLC0415

        with borrowed(
            None,
            lambda: _connect(self.connection_config, socket_timeout=WRITE_TIMEOUT_SECONDS),
        ) as conn:
            self.ensure_temp_schema(connection=conn)
            if custom_query:
                try:
                    statements = parse_read_only_statements(
                        custom_query, allowed_starts=frozenset({"SELECT", "WITH"})
                    )
                except ReadOnlySQLError as exc:
                    raise SQLRunnerError("custom_query must be a SELECT statement") from exc
                if len(statements) != 1:
                    raise SQLRunnerError("custom_query must be a SELECT statement")
                create_sql = (
                    f"CREATE TABLE {dest} {_COLUMN_MAPPING_TBLPROPERTIES} "
                    f"AS SELECT {self._row_id_select()} "
                    f"FROM ({statements[0]}) AS _src"
                )
                logger.info("Creating Databricks scratch table via CTAS: %s", temp_table)
                self._execute(create_sql, connection=conn)
                return (self.temp_schema, temp_table)

            if not source_table:
                raise SQLRunnerError("Either source_table or custom_query is required")
            src = self._source_ref(source_schema, source_table)
            # DEEP CLONE reproduces the source schema exactly, so it cannot carry
            # the row-id stamp that lets copies be aligned later. Materialization
            # pays for one CTAS scan; branching still uses DEEP CLONE and is cheap.
            logger.info("Creating Databricks scratch table via CTAS: %s", temp_table)
            self._execute(
                f"CREATE TABLE {dest} {_COLUMN_MAPPING_TBLPROPERTIES} "
                f"AS SELECT {self._row_id_select()} FROM {src} AS _src",
                connection=conn,
            )
        return (self.temp_schema, temp_table)

    # -- copies -------------------------------------------------------------

    def duplicate_temp_table(self, source_table: str, new_table: str) -> None:
        src = self.quote_relation(self.temp_schema, _sanitize_identifier(source_table))
        dst = self.quote_relation(self.temp_schema, _sanitize_identifier(new_table))
        self.ensure_temp_schema()
        try:
            self._execute(f"CREATE TABLE {dst} DEEP CLONE {src}")
        except SQLRunnerError:
            self._execute(f"CREATE TABLE {dst} AS SELECT * FROM {src}")

    # -- introspection ------------------------------------------------------

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = (
            f"SELECT table_name FROM {_qi(self.scratch_catalog)}.information_schema.tables "
            "WHERE table_schema = ? AND table_name LIKE ? ESCAPE '\\' LIMIT 200"
        )
        rows = self._execute_read(sql, (self.temp_schema, f"{escaped}%"))
        return [str(r["table_name"]) for r in rows]

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        sql = (
            f"SELECT column_name, data_type, is_nullable "
            f"FROM {_qi(self.scratch_catalog)}.information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position LIMIT 500"
        )
        rows = self._execute_read(sql, (schema, table))
        return [
            {
                "column_name": r["column_name"],
                "data_type": r["data_type"],
                "is_nullable": r["is_nullable"],
            }
            for r in rows
        ]

    def table_exists(self, schema: str, table: str) -> bool:
        sql = (
            f"SELECT 1 FROM {_qi(self.scratch_catalog)}.information_schema.tables "
            "WHERE table_schema = ? AND table_name = ? LIMIT 1"
        )
        return bool(self._execute_read(sql, (schema, table)))

    # -- reads --------------------------------------------------------------

    def run_sql_read_only(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
        del params  # Databricks runner binds with ?; callers currently pass no params
        try:
            statements = parse_read_only_statements(sql)
        except ReadOnlySQLError as exc:
            raise SQLRunnerError(str(exc)) from exc
        if len(statements) != 1:
            raise SQLRunnerError("Multiple statements not allowed")
        return self._execute_read_df(statements[0], timeout_seconds=WRITE_TIMEOUT_SECONDS)

    def delete_duplicates_keep_first(
        self,
        schema: str,
        table: str,
        subset_columns: Optional[List[str]] = None,
        sql_log: Optional[List[str]] = None,
    ) -> int:
        if schema != self.temp_schema:
            raise SQLRunnerError("delete_duplicates_keep_first only on the scratch schema")
        cols = self.get_table_columns(schema, table)
        col_names = [c["column_name"] for c in cols]
        subset = subset_columns or col_names
        subset_safe = [_sanitize_identifier(c) for c in subset if c in col_names]
        if not subset_safe:
            subset_safe = [_sanitize_identifier(c) for c in col_names]
        quoted = self.quote_relation(schema, table)
        before = self.get_row_count(schema, table)
        new_name = f"{table}_dedup_{uuid.uuid4().hex[:6]}"
        dest = self.quote_relation(schema, new_name)
        part = ", ".join(_qi(s) for s in subset_safe)
        create_sql = (
            f"CREATE TABLE {dest} {_COLUMN_MAPPING_TBLPROPERTIES} AS SELECT * FROM {quoted} "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {part}) = 1"
        )
        drop_sql = f"DROP TABLE {quoted}"
        rename_sql = f"ALTER TABLE {dest} RENAME TO {_qi(table)}"
        if sql_log is not None:
            sql_log.extend([create_sql, drop_sql, rename_sql])
        self._execute(create_sql)
        self._execute(drop_sql)
        self._execute(rename_sql)
        after = self.get_row_count(schema, table)
        return before - after

"""Scratch-schema SQL runner for Snowflake.

Working copies live in ``{database}.{scratch_schema}`` (default
``{connected_database}.STYCH_TEMP``) inside the customer's own account. The
source table is never mutated.

Snowflake specifics worth knowing:

- **Identifiers fold to upper case unless quoted.** Everything here is quoted,
  so a scratch schema created as ``"STYCH_TEMP"`` must always be referenced
  quoted too. The default is upper case so a quoted reference and an unquoted
  one resolve to the same object, which keeps hand-run SQL working.
- **DDL is auto-committed and each statement is its own transaction** unless an
  explicit BEGIN wraps them, so a failed sequence is not rolled back. Writes are
  confined to the scratch schema, so recovery is dropping the copy.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

import pandas as pd

from agents.sql_agent.runner import SQLRunnerError, _sanitize_identifier
from agents.sql_agent.warehouse_base import (
    WRITE_TIMEOUT_SECONDS,
    WarehouseSQLRunner,
    validated_scratch_schema,
)
from datasources.snowflake import (
    SnowflakeLoadError,
    execute_snowflake_statement,
    run_snowflake_query,
)
from datasources.sql_dialect import get_dialect

# Upper case so quoted and unquoted references resolve to the same schema.
DEFAULT_SCRATCH_SCHEMA = "STYCH_TEMP"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")


def scratch_schema(config: Mapping[str, Any]) -> str:
    return validated_scratch_schema(
        config,
        default=DEFAULT_SCRATCH_SCHEMA,
        pattern=_SCHEMA_RE,
        label="Snowflake scratch schema",
        hint="use letters, digits, '_' or '$'",
    )


class SnowflakeSQLRunner(WarehouseSQLRunner):
    """Clone source relations into a scratch schema and run SQL there."""

    engine_label = "Snowflake"
    write_fold = "upper"
    row_count_alias = "N"

    def __init__(self, connection_config: Mapping[str, Any]):
        cfg = dict(connection_config)
        self.connection_config = cfg
        self.database = str(cfg.get("database") or "").strip()
        if not self.database:
            raise SQLRunnerError(
                "Snowflake database is required for the scratch working copy — "
                "reconnect the table."
            )
        if not str(cfg.get("private_key") or "").strip():
            raise SQLRunnerError(
                "Snowflake credentials are unavailable for this session — please reconnect."
            )
        self._temp_schema = scratch_schema(cfg)
        self._dialect = get_dialect("snowflake")

    @property
    def credential_key(self) -> str:
        account = str(self.connection_config.get("account") or "")
        return f"{account}|{self.database}|{self._temp_schema}"

    def quote_relation(self, schema: str, table: str) -> str:
        return self._dialect.relation(schema, table, database=self.database)

    # -- execution ----------------------------------------------------------

    def _execute(self, sql: str, *, timeout_seconds: int = WRITE_TIMEOUT_SECONDS) -> None:
        try:
            execute_snowflake_statement(
                self.connection_config, sql, timeout_seconds=timeout_seconds
            )
        except SnowflakeLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc

    def _execute_read_df(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> pd.DataFrame:
        try:
            names, rows = run_snowflake_query(
                self.connection_config, sql, parameters, timeout_seconds=timeout_seconds
            )
        except SnowflakeLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return pd.DataFrame.from_records(rows, columns=names)

    def ensure_temp_schema(self) -> None:
        self._execute(
            f"CREATE SCHEMA IF NOT EXISTS {self._dialect.quote(self.database)}."
            f"{self._dialect.quote(self._temp_schema)}"
        )

    # -- copies -------------------------------------------------------------

    def duplicate_temp_table(self, source_table: str, new_table: str) -> None:
        src = self.quote_relation(self._temp_schema, _sanitize_identifier(source_table))
        dst = self.quote_relation(self._temp_schema, _sanitize_identifier(new_table))
        self.ensure_temp_schema()
        # CLONE is metadata-only and preserves the stamped row id, so branched
        # copies stay aligned with their parent for diffing.
        self._execute(f"CREATE TABLE {dst} CLONE {src}")

    # -- introspection ------------------------------------------------------

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        return self._table_names_from_frame(
            f"SELECT TABLE_NAME FROM {self._dialect.quote(self.database)}"
            ".INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ?",
            (self._temp_schema,),
            prefix,
        )

    def table_exists(self, schema: str, table: str) -> bool:
        return self._exists_from_frame(
            f"SELECT 1 FROM {self._dialect.quote(self.database)}"
            ".INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            (schema, table),
        )

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        return self._columns_from_frame(
            f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM {self._dialect.quote(self.database)}"
            ".INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
            "ORDER BY ORDINAL_POSITION",
            (schema, table),
        )

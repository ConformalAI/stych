"""Scratch-schema SQL runner for Amazon Redshift.

Working copies live in a Stych-owned schema (default ``stych_temp``) inside the
customer's own database. The source table is never mutated.

Redshift looks like Postgres and is not:

- **Authentication is IAM, through Amazon's own driver.** Credentials are
  short-lived and issued per connection, so there is no static URL and the
  shared ``PostgresSQLRunner`` cannot serve it.
- **There is no zero-copy clone.** Branching a working copy is a real
  ``CREATE TABLE AS SELECT *``, which carries the already-stamped row id
  forward, so copies stay aligned even though the data is rewritten.
- **``TEXT`` is an alias for ``VARCHAR(256)``**; the dialect casts to
  ``VARCHAR(65535)`` so long values are not silently truncated mid-comparison.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

import pandas as pd

from agents.sql_agent.runner import SQLRunnerError
from agents.sql_agent.warehouse_base import (
    WRITE_TIMEOUT_SECONDS,
    WarehouseSQLRunner,
    validated_scratch_schema,
)
from datasources.redshift import (
    RedshiftLoadError,
    execute_redshift_statement,
    run_redshift_query,
)
from datasources.sql_dialect import get_dialect

# Lower case: Redshift folds unquoted identifiers down, so a lower-case default
# makes a quoted and an unquoted reference resolve to the same schema.
DEFAULT_SCRATCH_SCHEMA = "stych_temp"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,126}$")


def scratch_schema(config: Mapping[str, Any]) -> str:
    return validated_scratch_schema(
        config,
        default=DEFAULT_SCRATCH_SCHEMA,
        pattern=_SCHEMA_RE,
        label="Redshift scratch schema",
        hint="use letters, digits, and underscores",
    )


class RedshiftSQLRunner(WarehouseSQLRunner):
    """Clone source relations into a scratch schema and run SQL there."""

    engine_label = "Redshift"

    def __init__(self, connection_config: Mapping[str, Any]):
        cfg = dict(connection_config)
        self.connection_config = cfg
        self.database = str(cfg.get("database") or "").strip()
        if not self.database:
            raise SQLRunnerError(
                "Redshift database is required for the scratch working copy — "
                "reconnect the table."
            )
        if not str(cfg.get("access_key_id") or "").strip():
            raise SQLRunnerError(
                "Redshift credentials are unavailable for this session — please reconnect."
            )
        self._temp_schema = scratch_schema(cfg)
        self._dialect = get_dialect("redshift")

    @property
    def credential_key(self) -> str:
        host = str(self.connection_config.get("host") or "")
        return f"{host}|{self.database}|{self._temp_schema}"

    def quote_relation(self, schema: str, table: str) -> str:
        # Redshift addresses schema.table; the database is fixed per connection.
        return self._dialect.relation(schema, table)

    # -- execution ----------------------------------------------------------

    def _execute(self, sql: str, *, timeout_seconds: int = WRITE_TIMEOUT_SECONDS) -> None:
        try:
            execute_redshift_statement(self.connection_config, sql, timeout_seconds=timeout_seconds)
        except RedshiftLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc

    def _execute_read_df(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> pd.DataFrame:
        try:
            names, rows = run_redshift_query(
                self.connection_config, sql, parameters, timeout_seconds=timeout_seconds
            )
        except RedshiftLoadError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return pd.DataFrame.from_records(rows, columns=names)

    def ensure_temp_schema(self) -> None:
        self._execute(f"CREATE SCHEMA IF NOT EXISTS {self._dialect.quote(self._temp_schema)}")

    # -- introspection ------------------------------------------------------

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        return self._table_names_from_frame(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (self._temp_schema,),
            prefix,
        )

    def table_exists(self, schema: str, table: str) -> bool:
        return self._exists_from_frame(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        return self._columns_from_frame(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )

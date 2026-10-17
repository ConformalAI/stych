"""Scratch-schema SQL runner for Microsoft SQL Server.

Working copies live in a Stych-owned schema (default ``stych_temp``) inside the
customer's own database. The source table is never mutated.

T-SQL diverges from every other engine here in three ways that reach the shared
code rather than staying local:

- **No ``CREATE TABLE AS SELECT``.** Materialization is ``SELECT ... INTO dest``,
  which the write guard treats as a write like any other — the target still has
  to be in the scratch schema.
- **No ``LIMIT``.** Row caps are a ``SELECT TOP n`` prefix, not a suffix.
- **No regular-expression operator.** Regex predicates are refused rather than
  approximated with ``LIKE``, which would quietly match different rows.

Unlike the cloud warehouses this connects with a SQLAlchemy URL, so it carries
its own credential handling instead of the shared ``connection_config`` path.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

import pandas as pd
from sqlalchemy import text

from agents.sql_agent.runner import SQLRunnerError
from agents.sql_agent.warehouse_base import (
    WRITE_TIMEOUT_SECONDS,
    WarehouseSQLRunner,
    validated_scratch_schema,
)
from datasources.sql_dialect import get_dialect

DEFAULT_SCRATCH_SCHEMA = "stych_temp"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,126}$")


def scratch_schema(config: Mapping[str, Any]) -> str:
    return validated_scratch_schema(
        config,
        default=DEFAULT_SCRATCH_SCHEMA,
        pattern=_SCHEMA_RE,
        label="SQL Server scratch schema",
        hint="use letters, digits, and underscores",
    )


class SqlServerSQLRunner(WarehouseSQLRunner):
    """Clone source relations into a scratch schema and run SQL there."""

    engine_label = "SQL Server"
    # A SQLAlchemy transaction wraps the batch, so a failed sequence rolls back.
    atomic_writes = True

    def __init__(self, connection_config: Mapping[str, Any]):
        cfg = dict(connection_config)
        self.connection_config = cfg
        url = str(cfg.get("connection_url") or "").strip()
        if not url or url == "[REDACTED]" or "://" not in url:
            raise SQLRunnerError(
                "The SQL Server connection for this source is unavailable — "
                "please reconnect the table."
            )
        self.connection_url = url
        self._temp_schema = scratch_schema(cfg)
        self._dialect = get_dialect("sqlserver")

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any]) -> "SqlServerSQLRunner":
        """SQL Server carries a URL rather than an encrypted config blob."""
        cfg: Dict[str, Any] = {"connection_url": meta.get("connection_url")}
        if meta.get("scratch_schema"):
            cfg["scratch_schema"] = meta["scratch_schema"]
        return cls(cfg)

    @property
    def credential_key(self) -> str:
        return f"{self.connection_url}|{self._temp_schema}"

    def quote_relation(self, schema: str, table: str) -> str:
        # SQL Server addresses schema.table; the database is fixed per connection.
        return self._dialect.relation(schema, table)

    # -- execution ----------------------------------------------------------

    def _engine(self):
        from agents.sql_agent.runner import get_shared_engine  # noqa: PLC0415

        return get_shared_engine(self.connection_url)

    def _execute(self, sql: str, *, timeout_seconds: int = WRITE_TIMEOUT_SECONDS) -> None:
        del timeout_seconds  # the pooled engine carries its own timeout
        try:
            with self._engine().begin() as conn:
                conn.execute(text(sql))
        except Exception as exc:  # noqa: BLE001 — surfaced as a runner error
            raise SQLRunnerError(str(exc)) from exc

    def _execute_read_df(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> pd.DataFrame:
        del timeout_seconds
        try:
            with self._engine().begin() as conn:
                result = conn.execute(text(sql), _named(parameters))
                return pd.DataFrame(result.fetchall(), columns=list(result.keys()))
        except Exception as exc:  # noqa: BLE001
            raise SQLRunnerError(str(exc)) from exc

    def execute_statements_transaction(
        self, statements: List[str], *, allowed_schema: str | None = None
    ) -> None:
        """Run every statement in one transaction, so a failure rolls back."""
        if not statements:
            return
        allowed = allowed_schema or self._temp_schema
        self._validate_write_statements(statements, allowed_schema=allowed)
        try:
            with self._engine().begin() as conn:
                for sql in statements:
                    conn.execute(text(sql))
        except SQLRunnerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SQLRunnerError(str(exc)) from exc

    def ensure_temp_schema(self) -> None:
        # T-SQL has no CREATE SCHEMA IF NOT EXISTS, and CREATE SCHEMA must be the
        # only statement in its batch — hence the dynamic EXEC.
        name = self._temp_schema.replace("'", "''")
        self._execute(
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{name}') "
            f"EXEC('CREATE SCHEMA {self._dialect.quote(self._temp_schema)}')"
        )

    # -- introspection ------------------------------------------------------

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]:
        return self._table_names_from_frame(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES " "WHERE TABLE_SCHEMA = :schema",
            {"schema": self._temp_schema},
            prefix,
        )

    def table_exists(self, schema: str, table: str) -> bool:
        return self._exists_from_frame(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :tname",
            {"schema": schema, "tname": table},
        )

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]:
        return self._columns_from_frame(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :tname ORDER BY ORDINAL_POSITION",
            {"schema": schema, "tname": table},
        )


def _named(parameters: Any) -> dict:
    """The shared base passes positional params; SQLAlchemy wants a mapping."""
    if isinstance(parameters, dict):
        return parameters
    return {}

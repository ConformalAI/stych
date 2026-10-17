"""Shared template-method base for the source-side warehouse runners.

Snowflake, Redshift, BigQuery, and Databricks all follow the same shape: clone
a source relation into a Stych-owned scratch schema, confine writes there, and
read results back. This module owns that skeleton so an engine port states only
what genuinely differs: credentials, connection handling, identifier quoting,
and clone DDL. ``runner.PostgresSQLRunner`` predates it and deliberately stands
apart (it also hosts the shared constants/helpers these runners import).

Subclass contract — after credential validation each subclass sets:

- ``connection_config`` — the driver's config mapping,
- ``_temp_schema`` — the resolved scratch schema name,
- ``_dialect`` — the engine's dialect (unless it overrides every method that
  touches it),
- ``engine_label`` / ``write_fold`` — how the engine is named in logs and how
  the write guard folds identifiers.

The base never opens a connection itself; ``_execute`` and ``_execute_read_df``
are the only engine touchpoints and are the abstract core of every port.
"""

from __future__ import annotations

import abc
import logging
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pandas as pd

from agents.sql_agent.runner import (
    ROW_ID_COLUMN,
    SQLRunnerError,
    _sanitize_identifier,
    temp_table_prefix,
)
from agents.sql_agent.write_guard import WriteGuardError, validate_write_statements
from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements

logger = logging.getLogger(__name__)

WRITE_TIMEOUT_SECONDS = 600


def validated_scratch_schema(
    config: Mapping[str, Any],
    *,
    default: str,
    pattern: "re.Pattern[str]",
    label: str,
    hint: str,
) -> str:
    """Resolve and validate the configured scratch-schema name.

    *label* names the engine and object (e.g. ``"Snowflake scratch schema"``);
    *hint* completes the error sentence.
    """
    name = str(config.get("scratch_schema") or default).strip()
    if not pattern.match(name):
        raise SQLRunnerError(f"Invalid {label} {name!r}: {hint}.")
    return name


class WarehouseSQLRunner(abc.ABC):
    """Clone source relations into a scratch schema and run SQL there."""

    #: Each statement commits on its own; a failed sequence is not rolled back.
    atomic_writes = False

    #: Engine name used in progress logs ("Snowflake", ...).
    engine_label: str = ""

    #: Identifier folding the write guard applies ("upper" on Snowflake).
    write_fold: str = "lower"

    #: Column alias for the COUNT(*) probe; case preserved per engine.
    row_count_alias: str = "n"

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any]) -> "WarehouseSQLRunner":
        from datasources.warehouse import WarehouseError, decode_connection_config  # noqa: PLC0415

        try:
            cfg = decode_connection_config(meta.get("connection_config"))
        except WarehouseError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return cls(cls._apply_meta_overrides(meta, cfg))

    @classmethod
    def _apply_meta_overrides(cls, meta: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
        """Return *cfg* with the dataset's per-attachment overrides applied.

        Never mutates *cfg*: a shared or cached config must not pick up one
        request's scratch schema.
        """
        out = dict(cfg)
        if meta.get("scratch_schema"):
            out["scratch_schema"] = meta["scratch_schema"]
        if meta.get("database"):
            out.setdefault("database", meta["database"])
        return out

    # -- identity -----------------------------------------------------------

    @property
    def temp_schema(self) -> str:
        return self._temp_schema

    def _row_id_select(self) -> str:
        return f"{self._dialect.row_id_expr()} AS {self._dialect.quote(ROW_ID_COLUMN)}, _src.*"

    # -- execution ----------------------------------------------------------

    @abc.abstractmethod
    def _execute(self, sql: str, *, timeout_seconds: int = WRITE_TIMEOUT_SECONDS) -> None:
        """Run one DDL/DML statement."""

    @abc.abstractmethod
    def _execute_read_df(
        self, sql: str, parameters: tuple[Any, ...] = (), *, timeout_seconds: int = 60
    ) -> pd.DataFrame:
        """Run one SELECT and return a small DataFrame."""

    def _ensure_scratch_for_copy(self, source_schema: str) -> None:
        """Prepare the scratch container before a copy (BigQuery co-locates it
        with *source_schema*; the others just create it idempotently)."""
        self.ensure_temp_schema()

    # -- copies -------------------------------------------------------------

    def create_temp_table_from_source(
        self,
        session_id: str,
        source_schema: str,
        source_table: Optional[str],
        custom_query: Optional[str],
        max_rows: int = 1_000_000,
        table_name: Optional[str] = None,
    ) -> Tuple[str, str]:
        del max_rows  # full-table clone; these source planes do not cap rows
        temp_table = _sanitize_identifier(table_name) if table_name else None
        if not temp_table:
            temp_table = f"stych_{temp_table_prefix(session_id)}{uuid.uuid4().hex[:8]}"
        self._ensure_scratch_for_copy(source_schema)
        dest = self.quote_relation(self._temp_schema, temp_table)

        if custom_query:
            try:
                statements = parse_read_only_statements(
                    custom_query, allowed_starts=frozenset({"SELECT", "WITH"})
                )
            except ReadOnlySQLError as exc:
                raise SQLRunnerError("custom_query must be a SELECT statement") from exc
            if len(statements) != 1:
                raise SQLRunnerError("custom_query must be a SELECT statement")
            src = f"({statements[0]}) AS _src"
        else:
            if not source_table:
                raise SQLRunnerError("Either source_table or custom_query is required")
            src = f"{self.quote_relation(source_schema, source_table)} AS _src"

        logger.info("Creating %s scratch table: %s", self.engine_label, temp_table)
        # The materialization form is the dialect's: CREATE TABLE AS on most
        # engines, SELECT ... INTO on T-SQL.
        self._execute(self._dialect.create_table_as(dest, self._row_id_select(), src))
        return (self._temp_schema, temp_table)

    def duplicate_temp_table(self, source_table: str, new_table: str) -> None:
        src = self.quote_relation(self._temp_schema, _sanitize_identifier(source_table))
        dst = self.quote_relation(self._temp_schema, _sanitize_identifier(new_table))
        self.ensure_temp_schema()
        # A plain copy carries the stamped row id forward rather than
        # re-generating it, so branched copies stay aligned for diffing.
        # Engines with zero-copy clones override this.
        self._execute(self._dialect.create_table_as(dst, "*", src))

    def drop_temp_table(self, table: str) -> None:
        ref = self.quote_relation(self._temp_schema, _sanitize_identifier(table))
        self._execute(self._dialect.drop_table_if_exists(ref))

    # -- introspection ------------------------------------------------------
    #
    # Engines exposing an ANSI information_schema probe it with different SQL
    # (casing, catalog qualifier, paramstyle) but shape results identically;
    # these helpers own the shaping so a port supplies only its statements.

    def _table_names_from_frame(self, sql: str, parameters: tuple, prefix: str) -> List[str]:
        frame = self._execute_read_df(sql, parameters)
        if frame.empty:
            return []
        return [name for name in frame[frame.columns[0]].astype(str) if name.startswith(prefix)]

    def _exists_from_frame(self, sql: str, parameters: tuple) -> bool:
        return not self._execute_read_df(sql, parameters).empty

    def _columns_from_frame(self, sql: str, parameters: tuple) -> List[Dict[str, Any]]:
        frame = self._execute_read_df(sql, parameters)
        if frame.empty:
            return []
        name_col, type_col = frame.columns[0], frame.columns[1]
        null_col = frame.columns[2] if len(frame.columns) > 2 else None
        return [
            {
                "column_name": str(row[name_col]),
                "data_type": str(row[type_col]),
                "is_nullable": str(row[null_col]) if null_col else "YES",
            }
            for _, row in frame.iterrows()
        ]

    # -- counts -------------------------------------------------------------

    def get_row_count(self, schema: str, table: str) -> int:
        frame = self._execute_read_df(
            f"SELECT COUNT(*) AS {self.row_count_alias} FROM "
            f"{self.quote_relation(schema, table)}"
        )
        return int(frame.iloc[0][frame.columns[0]]) if not frame.empty else 0

    # -- statements ---------------------------------------------------------

    def execute_statements_transaction(
        self, statements: List[str], *, allowed_schema: Optional[str] = None
    ) -> None:
        """Run write statements against the scratch schema.

        These engines auto-commit every statement, so this is sequential rather
        than atomic: a later failure leaves earlier statements applied. Every
        target is a scratch table, so recovery is dropping and rebuilding the
        copy.
        """
        if not statements:
            return
        allowed = allowed_schema or self._temp_schema
        self._validate_write_statements(statements, allowed_schema=allowed)
        for sql in statements:
            self._execute(sql)

    def _validate_write_statements(self, statements: List[str], *, allowed_schema: str) -> None:
        """Confine writes to the scratch schema (see ``write_guard``)."""
        try:
            validate_write_statements(
                statements, allowed_schema=allowed_schema, fold=self.write_fold
            )
        except WriteGuardError as exc:
            raise SQLRunnerError(str(exc)) from exc

    def run_sql_read_only(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
        del params  # warehouse reads here are fully-formed statements
        try:
            parse_read_only_statements(sql, allowed_starts=frozenset({"SELECT", "WITH"}))
        except ReadOnlySQLError as exc:
            raise SQLRunnerError(str(exc)) from exc
        return self._execute_read_df(sql)

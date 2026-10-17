"""A hardened, session-scoped DuckDB analytical execution kernel."""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeAlias

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads

from .deadline import run_with_deadline
from .naming import quote_identifier, validate_relation_name
from .security import DuckDBPathPolicy
from .types import (
    DuckDBColumn,
    DuckDBExecutionError,
    DuckDBInvalidQuery,
    DuckDBKernelClosed,
    DuckDBKernelConfig,
    DuckDBQueryResult,
    DuckDBQueryTimeout,
    DuckDBRelation,
    DuckDBRelationExists,
    DuckDBUnsafeQuery,
    QueryParameters,
)

DuckDBViewInput: TypeAlias = pd.DataFrame | pa.Table | pads.Dataset
DuckDBMaterializedInput: TypeAlias = DuckDBViewInput | pa.RecordBatch | pa.RecordBatchReader


class DuckDBKernel:
    """One isolated DuckDB connection and catalog for one Stych session.

    The connection is deliberately not the DuckDB module-global connection.
    A re-entrant lock serializes access because DuckDB connection handles cannot
    execute concurrently. Inputs are explicitly registered; Python replacement
    scans, extension loading, network/filesystem access, and config changes are
    disabled before any caller-provided SQL can execute.
    """

    def __init__(
        self,
        session_id: str,
        temp_directory: Path,
        config: DuckDBKernelConfig | None = None,
        allowed_data_roots: Sequence[Path] | None = None,
    ) -> None:
        self.session_id = session_id
        self.config = config or DuckDBKernelConfig()
        self.config.validate()
        self.temp_directory = Path(temp_directory).resolve()
        self.temp_directory.mkdir(parents=True, exist_ok=True)
        self.path_policy = DuckDBPathPolicy(
            allowed_data_roots if allowed_data_roots is not None else [self.temp_directory.parent]
        )
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._relations: dict[str, DuckDBRelation] = {}
        self._registered_objects: dict[str, tuple[str, Any]] = {}
        try:
            self._connection = duckdb.connect(
                ":memory:",
                config={
                    "threads": str(self.config.threads),
                    "memory_limit": self.config.memory_limit,
                    "temp_directory": str(self.temp_directory),
                    "max_temp_directory_size": self.config.max_temp_directory_size,
                    "allow_community_extensions": "false",
                    "autoinstall_known_extensions": "false",
                    "autoload_known_extensions": "false",
                    "preserve_insertion_order": "false",
                },
            )
            self._connection.execute("SET python_enable_replacements = false")
            self._connection.execute("SET enable_external_access = false")
            self._connection.execute("SET lock_configuration = true")
        except Exception as exc:
            raise DuckDBExecutionError(f"Could not initialize DuckDB: {exc}") from exc

    @property
    def closed(self) -> bool:
        return self._closed or self._closing

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise DuckDBKernelClosed(f"DuckDB kernel for session {self.session_id} is closed")

    def _schema_for(self, name: str) -> tuple[DuckDBColumn, ...]:
        table = self._connection.sql(
            f"SELECT * FROM {quote_identifier(name)} LIMIT 0"
        ).to_arrow_table()
        return tuple(DuckDBColumn(field.name, str(field.type)) for field in table.schema)

    def _drop_locked(self, name: str) -> bool:
        existing = self._relations.get(name)
        if existing is None:
            return False
        if existing.kind == "view":
            self._connection.execute(f"DROP VIEW {quote_identifier(name)}")
            registered = self._registered_objects.pop(name, None)
            if registered is not None:
                self._connection.unregister(registered[0])
        else:
            self._connection.execute(f"DROP TABLE {quote_identifier(name)}")
        self._relations.pop(name, None)
        return True

    def _replace_catalog_relation_locked(self, name: str, create_sql: str) -> None:
        existing = self._relations.get(name)
        old_registered = self._registered_objects.get(name)
        self._connection.execute("BEGIN TRANSACTION")
        try:
            if existing is not None:
                kind = "VIEW" if existing.kind == "view" else "TABLE"
                self._connection.execute(f"DROP {kind} {quote_identifier(name)}")
            self._connection.execute(create_sql)
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        if old_registered is not None:
            try:
                self._connection.unregister(old_registered[0])
            except Exception:
                pass
            self._registered_objects.pop(name, None)

    def _validate_input(self, data: Any) -> None:
        if isinstance(data, pads.Dataset):
            self.path_policy.authorize_dataset(data)

    def register_view(
        self,
        name: str,
        data: DuckDBViewInput,
        *,
        replace: bool = False,
        source: str | None = None,
    ) -> DuckDBRelation:
        """Register a read-only view over a pandas or Arrow object."""
        name = validate_relation_name(name)
        with self._lock:
            self._ensure_open()
            if name in self._relations and not replace:
                raise DuckDBRelationExists(f"Relation '{name}' already exists")
            self._validate_input(data)
            internal_name = f"_stych_view_{uuid.uuid4().hex}"
            try:
                self._connection.register(internal_name, data)
                relation = DuckDBRelation(
                    name=name,
                    kind="view",
                    columns=self._schema_for(internal_name),
                    rows=_known_rows(data),
                    source=source,
                )
                self._replace_catalog_relation_locked(
                    name,
                    f"CREATE VIEW {quote_identifier(name)} AS "
                    f"SELECT * FROM {quote_identifier(internal_name)}",
                )
            except Exception as exc:
                try:
                    self._connection.unregister(internal_name)
                except Exception:
                    pass
                raise DuckDBExecutionError(f"Could not register relation '{name}': {exc}") from exc
            self._registered_objects[name] = (internal_name, data)
            self._relations[name] = relation
            return relation

    def register_parquet(
        self,
        name: str,
        path: Path,
        *,
        replace: bool = False,
        source: str | None = None,
    ) -> DuckDBRelation:
        """Register a lazy Arrow dataset; DuckDB itself receives no filesystem access."""
        resolved = self.path_policy.authorize_file(path)
        try:
            dataset = pads.dataset(str(resolved), format="parquet")
        except Exception as exc:
            raise DuckDBExecutionError(f"Could not open Parquet data: {exc}") from exc
        return self.register_view(name, dataset, replace=replace, source=source or str(resolved))

    def materialize(
        self,
        name: str,
        data: DuckDBMaterializedInput,
        *,
        replace: bool = False,
        source: str | None = None,
        timeout_seconds: float | None = None,
    ) -> DuckDBRelation:
        """Copy an input into a stable native DuckDB table."""
        name = validate_relation_name(name)
        staging_input = f"_stych_ingest_{uuid.uuid4().hex}"
        staging_table = f"_stych_table_{uuid.uuid4().hex}"
        with self._lock:
            self._ensure_open()
            if name in self._relations and not replace:
                raise DuckDBRelationExists(f"Relation '{name}' already exists")
            self._validate_input(data)
            deadline = (
                timeout_seconds
                if timeout_seconds is not None
                else self.config.ingest_timeout_seconds
            )
            if deadline <= 0:
                raise ValueError("timeout_seconds must be greater than 0")
            try:
                self._connection.register(staging_input, data)
                run_with_deadline(
                    self._connection,
                    lambda: self._connection.execute(
                        f"CREATE TABLE {quote_identifier(staging_table)} AS "
                        f"SELECT * FROM {quote_identifier(staging_input)}"
                    ),
                    deadline,
                    label="DuckDB ingestion",
                )
                relation = DuckDBRelation(
                    name=name,
                    kind="table",
                    columns=self._schema_for(staging_table),
                    rows=_known_rows(data),
                    source=source,
                )
                self._replace_catalog_relation_locked(
                    name,
                    f"ALTER TABLE {quote_identifier(staging_table)} "
                    f"RENAME TO {quote_identifier(name)}",
                )
            except Exception as exc:
                try:
                    self._connection.execute(
                        f"DROP TABLE IF EXISTS {quote_identifier(staging_table)}"
                    )
                except Exception:
                    pass
                if isinstance(exc, DuckDBQueryTimeout):
                    raise
                raise DuckDBExecutionError(
                    f"Could not materialize relation '{name}': {exc}"
                ) from exc
            finally:
                try:
                    self._connection.unregister(staging_input)
                except Exception:
                    pass
            self._relations[name] = relation
            return relation

    def query(
        self,
        sql: str,
        parameters: QueryParameters = None,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> DuckDBQueryResult:
        """Execute one read-only statement and return a bounded Arrow table."""
        row_cap = max_rows if max_rows is not None else self.config.max_result_rows
        if row_cap < 1:
            raise ValueError("max_rows must be at least 1")
        deadline = (
            timeout_seconds if timeout_seconds is not None else self.config.query_timeout_seconds
        )
        if deadline <= 0:
            raise ValueError("timeout_seconds must be greater than 0")

        with self._lock:
            self._ensure_open()
            statement = self._validate_query(sql)
            started = time.monotonic()
            try:
                table = run_with_deadline(
                    self._connection,
                    lambda: self._connection.sql(statement.query, params=parameters)
                    .limit(row_cap + 1)
                    .to_arrow_table(batch_size=min(row_cap + 1, 65_536)),
                    deadline,
                    label="DuckDB query",
                )
            except DuckDBQueryTimeout:
                raise
            except Exception as exc:
                raise DuckDBExecutionError(f"DuckDB query failed: {exc}") from exc
            truncated = table.num_rows > row_cap
            if truncated:
                table = table.slice(0, row_cap)
            return DuckDBQueryResult(
                table=table,
                truncated=truncated,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

    def _validate_query(self, sql: str):
        if not isinstance(sql, str) or not sql.strip():
            raise DuckDBInvalidQuery("SQL cannot be empty")
        try:
            statements = self._connection.extract_statements(sql)
        except Exception as exc:
            raise DuckDBInvalidQuery(f"Could not parse SQL: {exc}") from exc
        if len(statements) != 1:
            raise DuckDBUnsafeQuery("Exactly one SQL statement is allowed")
        statement = statements[0]
        if statement.type != duckdb.StatementType.SELECT:
            raise DuckDBUnsafeQuery("Only read-only SELECT-family statements are allowed")
        return statement

    def relations(self) -> tuple[DuckDBRelation, ...]:
        with self._lock:
            self._ensure_open()
            return tuple(self._relations.values())

    def remove_relation(self, name: str) -> bool:
        name = validate_relation_name(name)
        with self._lock:
            self._ensure_open()
            return self._drop_locked(name)

    def interrupt(self) -> None:
        """Interrupt the currently executing query without waiting on its lock."""
        with self._lifecycle_lock:
            if not self._closed:
                self._connection.interrupt()

    def close(self) -> None:
        close_error: Exception | None = None
        with self._lifecycle_lock:
            if self._closed or self._closing:
                return
            self._closing = True
            try:
                self._connection.interrupt()
            except Exception:
                pass
            with self._lock:
                try:
                    self._connection.close()
                except Exception as exc:
                    close_error = exc
                finally:
                    self._registered_objects.clear()
                    self._relations.clear()
                    self._closed = True
                    self._closing = False
        try:
            shutil.rmtree(self.temp_directory, ignore_errors=True)
        except OSError:
            pass
        if close_error is not None:
            raise DuckDBExecutionError(
                f"Could not close DuckDB kernel: {close_error}"
            ) from close_error

    def __enter__(self) -> "DuckDBKernel":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _known_rows(data: Any) -> int | None:
    if isinstance(data, pd.DataFrame):
        return len(data)
    if isinstance(data, pa.Table):
        return int(data.num_rows)
    if isinstance(data, pa.RecordBatch):
        return int(data.num_rows)
    return None

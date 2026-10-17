"""Typed inputs, outputs, and errors for the DuckDB execution kernel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pyarrow as pa

QueryParameters = Mapping[str, Any] | Sequence[Any] | None


class DuckDBKernelError(RuntimeError):
    """Base error for the local analytical execution plane."""


class DuckDBKernelClosed(DuckDBKernelError):
    """The kernel cannot accept work after it has been closed."""


class DuckDBInvalidQuery(DuckDBKernelError):
    """The SQL is empty, invalid, multi-statement, or not read-only."""


class DuckDBUnsafeQuery(DuckDBInvalidQuery):
    """Only a single read-only SELECT-family statement may run."""


class DuckDBExecutionError(DuckDBKernelError):
    """DuckDB failed while executing an otherwise valid operation."""


class DuckDBQueryTimeout(DuckDBExecutionError):
    """The watchdog interrupted a query after its deadline."""


class DuckDBRelationExists(DuckDBKernelError):
    """A registered or materialized relation already uses this name."""


class DuckDBInvalidRelationName(DuckDBKernelError):
    """Relation names must be safe, stable SQL identifiers."""


class DuckDBUnauthorizedPath(DuckDBKernelError):
    """A file-backed input is outside the kernel's authorized workspace roots."""


@dataclass(frozen=True)
class DuckDBKernelConfig:
    memory_limit: str = "512MB"
    threads: int = 2
    max_result_rows: int = 10_000
    max_temp_directory_size: str = "1GB"
    query_timeout_seconds: float = 60.0
    ingest_timeout_seconds: float = 120.0

    def validate(self) -> None:
        if self.threads < 1:
            raise ValueError("threads must be at least 1")
        if self.max_result_rows < 1:
            raise ValueError("max_result_rows must be at least 1")
        if not self.memory_limit.strip():
            raise ValueError("memory_limit cannot be empty")
        if not self.max_temp_directory_size.strip():
            raise ValueError("max_temp_directory_size cannot be empty")
        if self.query_timeout_seconds <= 0:
            raise ValueError("query_timeout_seconds must be greater than 0")
        if self.ingest_timeout_seconds <= 0:
            raise ValueError("ingest_timeout_seconds must be greater than 0")


@dataclass(frozen=True)
class DuckDBColumn:
    name: str
    data_type: str


@dataclass(frozen=True)
class DuckDBRelation:
    name: str
    kind: str  # "view" | "table"
    columns: tuple[DuckDBColumn, ...]
    rows: int | None = None
    source: str | None = None


@dataclass(frozen=True)
class DuckDBQueryResult:
    table: pa.Table
    truncated: bool
    elapsed_ms: float

    @property
    def row_count(self) -> int:
        return int(self.table.num_rows)

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(self.table.column_names)

    def to_pandas(self):
        return self.table.to_pandas()


@dataclass(frozen=True)
class DuckDBKernelLocation:
    session_id: str
    user_id: str
    temp_directory: Path

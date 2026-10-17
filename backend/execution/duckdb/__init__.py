"""Session-scoped DuckDB analytical execution kernel."""

from .kernel import DuckDBKernel
from .manager import DuckDBKernelManager, duckdb_kernel_manager
from .naming import source_relation_name, validate_relation_name
from .types import (
    DuckDBColumn,
    DuckDBExecutionError,
    DuckDBInvalidQuery,
    DuckDBInvalidRelationName,
    DuckDBKernelClosed,
    DuckDBKernelConfig,
    DuckDBKernelError,
    DuckDBQueryResult,
    DuckDBQueryTimeout,
    DuckDBRelation,
    DuckDBRelationExists,
    DuckDBUnauthorizedPath,
    DuckDBUnsafeQuery,
)

__all__ = [
    "DuckDBColumn",
    "DuckDBExecutionError",
    "DuckDBInvalidQuery",
    "DuckDBInvalidRelationName",
    "DuckDBKernel",
    "DuckDBKernelClosed",
    "DuckDBKernelConfig",
    "DuckDBKernelError",
    "DuckDBKernelManager",
    "DuckDBQueryResult",
    "DuckDBQueryTimeout",
    "DuckDBRelation",
    "DuckDBRelationExists",
    "DuckDBUnsafeQuery",
    "DuckDBUnauthorizedPath",
    "duckdb_kernel_manager",
    "source_relation_name",
    "validate_relation_name",
]

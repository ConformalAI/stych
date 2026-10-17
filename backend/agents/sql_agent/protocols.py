"""The scratch-schema runner boundary, in one place.

Postgres and Databricks already expose the same duck-typed surface — clone a
source relation into a Stych-owned scratch schema, run DML there, read results
back. This names that surface and centralizes selection, so a third source-side
connector is one registry entry rather than another ``if source == ...`` branch
scattered through the agent, the lifecycle cleanup, and the join planner.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, runtime_checkable

import pandas as pd

from .runner import PostgresSQLRunner, SQLRunnerError


@runtime_checkable
class ScratchSQLRunner(Protocol):
    """Read/write access to one engine, with writes confined to a scratch schema."""

    @property
    def temp_schema(self) -> str: ...

    @property
    def credential_key(self) -> str: ...

    def quote_relation(self, schema: str, table: str) -> str: ...

    def ensure_temp_schema(self) -> None: ...

    def create_temp_table_from_source(self, *args: Any, **kwargs: Any) -> Any: ...

    def drop_temp_table(self, table: str) -> None: ...

    def table_exists(self, schema: str, table: str) -> bool: ...

    def get_row_count(self, schema: str, table: str) -> int: ...

    def get_table_columns(self, schema: str, table: str) -> List[Dict[str, Any]]: ...

    def execute_statements_transaction(self, *args: Any, **kwargs: Any) -> Any: ...

    def run_sql_read_only(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame: ...

    def duplicate_temp_table(self, source_table: str, new_table: str) -> None: ...

    def list_temp_tables_with_prefix(self, prefix: str) -> List[str]: ...

    #: True only where a failed statement sequence rolls back. Callers that need
    #: two statements to land together must check this rather than assume it.
    atomic_writes: bool

    def create_or_replace_view(self, *args: Any, **kwargs: Any) -> Any:
        """Publish a view over a copy. Not every engine exposes this."""
        raise NotImplementedError

    def delete_duplicates_keep_first(self, *args: Any, **kwargs: Any) -> Any:
        """Engine-native de-duplication. Not every engine implements it."""
        raise NotImplementedError


def _postgres_runner(meta: Mapping[str, Any]) -> ScratchSQLRunner:
    url = meta.get("connection_url")
    if not url or str(url) == "[REDACTED]":
        raise SQLRunnerError(
            "The database connection for this source is unavailable — please reconnect the table."
        )
    return PostgresSQLRunner(str(url))


def _bigquery_runner(meta: Mapping[str, Any]) -> ScratchSQLRunner:
    from .bigquery_runner import BigQuerySQLRunner  # noqa: PLC0415 — optional dependency

    return BigQuerySQLRunner.from_meta(meta)


def _snowflake_runner(meta: Mapping[str, Any]) -> ScratchSQLRunner:
    from .snowflake_runner import SnowflakeSQLRunner  # noqa: PLC0415 — optional dependency

    return SnowflakeSQLRunner.from_meta(meta)


def _redshift_runner(meta: Mapping[str, Any]) -> ScratchSQLRunner:
    from .redshift_runner import RedshiftSQLRunner  # noqa: PLC0415 — optional dependency

    return RedshiftSQLRunner.from_meta(meta)


def _sqlserver_runner(meta: Mapping[str, Any]) -> ScratchSQLRunner:
    from .sqlserver_runner import SqlServerSQLRunner  # noqa: PLC0415 — optional dependency

    return SqlServerSQLRunner.from_meta(meta)


def _databricks_runner(meta: Mapping[str, Any]) -> ScratchSQLRunner:
    from .databricks_runner import DatabricksSQLRunner  # noqa: PLC0415 — optional dependency

    return DatabricksSQLRunner.from_meta(meta)


# Connectors that execute Stych's SQL inside the customer's own engine. A
# connector absent here has no source-side runner and is never chosen for
# source-side work; nothing infers a runner from a connection URL alone.
_RUNNER_FACTORIES: Dict[str, Callable[[Mapping[str, Any]], ScratchSQLRunner]] = {
    "postgres": _postgres_runner,
    "databricks": _databricks_runner,
    "bigquery": _bigquery_runner,
    "snowflake": _snowflake_runner,
    "redshift": _redshift_runner,
    "sqlserver": _sqlserver_runner,
}


def supports_capability(runner: Any, name: str) -> bool:
    """True when *runner* actually implements an optional Protocol member.

    The Protocol declares the full surface so callers can discover it; the
    optional members raise ``NotImplementedError`` by default, so asking before
    calling is how a caller stays correct across engines.
    """
    method = getattr(type(runner), name, None)
    if method is None:
        return False
    return getattr(method, "__qualname__", "").split(".")[0] != "ScratchSQLRunner"


def supports_source_execution(meta: Optional[Mapping[str, Any]]) -> bool:
    """True when this context's connector can run Stych SQL in the source."""
    return bool(meta) and str(meta.get("source") or "") in _RUNNER_FACTORIES


def runner_for(meta: Optional[Mapping[str, Any]]) -> ScratchSQLRunner:
    """Build the scratch runner for *meta*, or fail with a user-facing reason."""
    source = str((meta or {}).get("source") or "")
    factory = _RUNNER_FACTORIES.get(source)
    if factory is None:
        raise SQLRunnerError(f"{source or 'This source'} does not run SQL inside the source.")
    return factory(meta or {})

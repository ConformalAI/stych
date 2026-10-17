"""
Table adapters: pushdown preview/query layer over foreign database sources.

``resolve_adapter(ctx, variant)`` maps a session ``DataContext`` (whose
metadata carries the connection info stored at connect time) to the right
backend adapter:

- ``variant="base"``        → the user's own table / collection / custom query.
- ``variant="temp"``        → the *active* scratch working copy (Postgres / Databricks).
- ``variant="copy:<name>"`` → a specific named working copy (Postgres / Databricks).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, List

import pandas as pd

from .base import (
    DIFF_TIMEOUT_MS,
    PREVIEW_TIMEOUT_MS,
    SAMPLE_ROWS,
    AdapterColumn,
    AdapterError,
    AdapterTimeout,
    AdapterUnavailable,
    CountResult,
    SourceCacheInfo,
    TableAdapter,
    TempTableUnavailable,
    cached_count,
    clear_caches,
)
from .bigquery import BigQueryTableAdapter
from .databricks import DatabricksTableAdapter
from .duckdb import DuckDBTableAdapter
from .mongodb import MongoTableAdapter
from .postgres import PostgresTableAdapter
from .redshift import RedshiftTableAdapter
from .snowflake import SnowflakeTableAdapter
from .sqlserver import SqlServerTableAdapter

__all__ = [
    "AdapterColumn",
    "AdapterError",
    "AdapterTimeout",
    "AdapterUnavailable",
    "CountResult",
    "DIFF_TIMEOUT_MS",
    "BigQueryTableAdapter",
    "DatabricksTableAdapter",
    "DuckDBTableAdapter",
    "MongoTableAdapter",
    "PREVIEW_TIMEOUT_MS",
    "PostgresTableAdapter",
    "RedshiftTableAdapter",
    "SAMPLE_ROWS",
    "SourceCacheInfo",
    "SqlServerTableAdapter",
    "SnowflakeTableAdapter",
    "TableAdapter",
    "TempTableUnavailable",
    "cached_count",
    "clear_caches",
    "resolve_adapter",
    "supports_pushdown",
]

# Base tables can change under us in the client's database — keep counts fresh.
# The working copy only changes when the agent mutates it, and every mutation
# bumps temp_table_generation (a new cache key), so a longer TTL is safe.
_BASE_TTL_SECONDS = 60.0
_TEMP_TTL_SECONDS = 300.0

_DB_SOURCES = {
    "postgres",
    "sqlserver",
    "mongodb",
    "redshift",
    "snowflake",
    "bigquery",
    "databricks",
}
_WAREHOUSE_SOURCES = {"redshift", "snowflake", "bigquery", "databricks"}


def _connection_url(meta: dict) -> str:
    url = (meta.get("connection_url") or "").strip()
    if not url or url == "[REDACTED]" or "://" not in url:
        raise AdapterUnavailable(
            "Database connection is unavailable for this session — please reconnect the table."
        )
    return url


def _connection_config(meta: dict) -> dict[str, Any]:
    from datasources.warehouse import WarehouseError, decode_connection_config  # noqa: PLC0415

    try:
        return decode_connection_config(meta.get("connection_config"))
    except WarehouseError as exc:
        raise AdapterUnavailable(str(exc)) from exc


def _credential_cache_key(config: dict[str, Any]) -> str:
    serialized = json.dumps(config, sort_keys=True, default=str).encode()
    return hashlib.sha256(serialized).hexdigest()[:16]


def _frame_kind(dtype: Any) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date"
    return "string"


def _sample_columns(ctx: Any) -> List[AdapterColumn]:
    """Column set retained as source metadata when a DB has no catalog schema."""
    metadata_columns = (getattr(ctx, "metadata", None) or {}).get("source_columns") or []
    if metadata_columns:
        return [
            AdapterColumn(
                name=str(column["name"]),
                db_type=str(column.get("db_type") or ""),
                kind=str(column.get("kind") or "string"),
            )
            for column in metadata_columns
            if isinstance(column, dict) and column.get("name")
        ]
    df = getattr(ctx, "working_dataframe", None)
    if df is None:
        return []
    return [
        AdapterColumn(name=str(col), db_type=str(dtype), kind=_frame_kind(dtype))
        for col, dtype in df.dtypes.items()
    ]


def supports_pushdown(meta: dict) -> bool:
    """True when this context's source can serve a full-table preview."""
    if (meta or {}).get("source") not in _DB_SOURCES:
        return False
    try:
        if (meta or {}).get("source") in _WAREHOUSE_SOURCES:
            _connection_config(meta or {})
        else:
            _connection_url(meta or {})
        return True
    except AdapterUnavailable:
        return False


def temp_generation(meta: dict) -> int:
    return int((meta or {}).get("temp_table_generation") or 0)


def _warehouse_temp_adapter(
    source: str,
    ctx: Any,
    connection_config: dict[str, Any],
    *,
    meta: dict,
    temp_schema: str,
    temp_table: str,
) -> TableAdapter:
    """Adapter over a warehouse working copy, in the scratch schema.

    Each engine names the enclosing container differently — a BigQuery dataset
    sits under a project, a Snowflake schema under a database, a Redshift schema
    under the connected database — so the qualifier is resolved per connector
    rather than assumed.
    """
    builders = {
        "snowflake": (SnowflakeTableAdapter, connection_config.get("database")),
        "redshift": (RedshiftTableAdapter, None),
        "bigquery": (
            BigQueryTableAdapter,
            connection_config.get("project_id") or connection_config.get("database"),
        ),
    }
    adapter_cls, database = builders[source]
    return adapter_cls(
        connection_config,
        database=database,
        schema=temp_schema,
        table=temp_table,
        custom_query=None,
        sample_kinds={c.name: c.kind for c in _sample_columns(ctx)},
        cache_info=SourceCacheInfo(
            key=(
                source,
                _credential_cache_key(connection_config),
                str(database or ""),
                temp_schema,
                temp_table,
                temp_generation(meta),
            ),
            ttl=_TEMP_TTL_SECONDS,
        ),
    )


def resolve_adapter(ctx: Any, variant: str = "base") -> TableAdapter:
    meta = ctx.metadata or {}
    source = meta.get("source")
    if source not in _DB_SOURCES:
        raise AdapterError(f"Source '{source}' is not a database source.")
    url = None
    connection_config = None
    if source in _WAREHOUSE_SOURCES:
        connection_config = _connection_config(meta)
    else:
        url = _connection_url(meta)

    if variant == "temp" or variant.startswith("copy:"):
        from datasources.registry import SOURCE_WORKING_COPY_IDS  # noqa: PLC0415

        if source not in SOURCE_WORKING_COPY_IDS:
            supported = ", ".join(sorted(SOURCE_WORKING_COPY_IDS))
            raise AdapterError(
                f"Working-copy access is not supported for {source} sources. "
                f"Supported: {supported}."
            )
        if variant == "temp":
            # The active working copy (legacy keys mirror the registry).
            temp_schema, temp_table = meta.get("temp_schema"), meta.get("temp_table")
            if not temp_schema or not temp_table:
                raise TempTableUnavailable(
                    "No working table exists yet — it is created when an agent first runs SQL."
                )
        else:
            from agents.sql_agent import working_copies as wc  # noqa: PLC0415

            name = variant[len("copy:") :]
            entry = wc.get_copy(meta, name)
            if entry is None:
                raise TempTableUnavailable(f"No working copy named '{name}' exists.")
            temp_schema, temp_table = wc.scratch_schema(meta), entry["table"]
        if source == "snowflake":
            # Snowflake's pure resolver owns the physical namespace spelling.
            # Its default is STYCH_TEMP; treating the connector-neutral
            # lowercase fallback as equivalent makes quoted reads miss a table
            # that does exist.  Do not construct a live runner here: adapter
            # resolution must not validate unrelated credentials or clients.
            from agents.sql_agent import working_copies as wc  # noqa: PLC0415
            from agents.sql_agent.snowflake_runner import (  # noqa: PLC0415
                scratch_schema as snowflake_scratch_schema,
            )

            assert connection_config is not None
            physical_schema = snowflake_scratch_schema(connection_config)
            wc.bind_scratch_schema(meta, physical_schema)
            temp_schema = physical_schema
        if source == "databricks":
            assert connection_config is not None
            scratch_catalog = (
                meta.get("scratch_catalog")
                or connection_config.get("scratch_catalog")
                or connection_config.get("catalog")
            )
            return DatabricksTableAdapter(
                connection_config,
                database=scratch_catalog,
                schema=temp_schema,
                table=temp_table,
                custom_query=None,
                sample_kinds={c.name: c.kind for c in _sample_columns(ctx)},
                cache_info=SourceCacheInfo(
                    key=(
                        "databricks",
                        _credential_cache_key(connection_config),
                        scratch_catalog,
                        temp_schema,
                        temp_table,
                        temp_generation(meta),
                    ),
                    ttl=_TEMP_TTL_SECONDS,
                ),
            )
        if source == "mongodb":
            # Without this the working copy fell through to the Postgres adapter
            # below and was handed a mongodb:// URL.
            return MongoTableAdapter(
                url,
                database=temp_schema,
                collection=temp_table,
                base_query=None,
                columns=_sample_columns(ctx),
                cache_info=SourceCacheInfo(
                    key=("mongo", url, temp_schema, temp_table, temp_generation(meta)),
                    ttl=_TEMP_TTL_SECONDS,
                ),
            )
        if source == "sqlserver":
            return SqlServerTableAdapter(
                url,
                schema=temp_schema,
                table=temp_table,
                custom_query=None,
                sample_kinds={c.name: c.kind for c in _sample_columns(ctx)},
                cache_info=SourceCacheInfo(
                    key=("mssql", url, temp_schema, temp_table, temp_generation(meta)),
                    ttl=_TEMP_TTL_SECONDS,
                ),
            )
        if source in _WAREHOUSE_SOURCES:
            assert connection_config is not None
            return _warehouse_temp_adapter(
                source,
                ctx,
                connection_config,
                meta=meta,
                temp_schema=temp_schema,
                temp_table=temp_table,
            )
        return PostgresTableAdapter(
            url,
            schema=temp_schema,
            table=temp_table,
            cache_info=SourceCacheInfo(
                key=("pg", url, f"{temp_schema}.{temp_table}", temp_generation(meta)),
                ttl=_TEMP_TTL_SECONDS,
            ),
        )

    if variant != "base":
        raise AdapterError(f"Unknown source variant '{variant}'.")

    if source == "postgres":
        return PostgresTableAdapter(
            url,
            schema=meta.get("schema", "public"),
            table=meta.get("table"),
            custom_query=meta.get("custom_query"),
            cache_info=SourceCacheInfo(
                key=("pg", url, meta.get("schema"), meta.get("table"), meta.get("custom_query")),
                ttl=_BASE_TTL_SECONDS,
            ),
        )
    if source == "sqlserver":
        sample_kinds = {c.name: c.kind for c in _sample_columns(ctx)}
        return SqlServerTableAdapter(
            url,
            schema=meta.get("schema", "dbo"),
            table=meta.get("table"),
            custom_query=meta.get("custom_query"),
            sample_kinds=sample_kinds,
            cache_info=SourceCacheInfo(
                key=("mssql", url, meta.get("schema"), meta.get("table"), meta.get("custom_query")),
                ttl=_BASE_TTL_SECONDS,
            ),
        )
    if source == "snowflake":
        sample_kinds = {c.name: c.kind for c in _sample_columns(ctx)}
        assert connection_config is not None
        return SnowflakeTableAdapter(
            connection_config,
            database=meta.get("database"),
            schema=meta.get("schema", "PUBLIC"),
            table=meta.get("table"),
            custom_query=meta.get("custom_query"),
            sample_kinds=sample_kinds,
            cache_info=SourceCacheInfo(
                key=(
                    "snowflake",
                    _credential_cache_key(connection_config),
                    meta.get("database"),
                    meta.get("schema"),
                    meta.get("table"),
                    meta.get("custom_query"),
                ),
                ttl=_BASE_TTL_SECONDS,
            ),
        )
    if source == "redshift":
        sample_kinds = {c.name: c.kind for c in _sample_columns(ctx)}
        assert connection_config is not None
        return RedshiftTableAdapter(
            connection_config,
            database=None,
            schema=meta.get("schema", "public"),
            table=meta.get("table"),
            custom_query=meta.get("custom_query"),
            sample_kinds=sample_kinds,
            cache_info=SourceCacheInfo(
                key=(
                    "redshift",
                    _credential_cache_key(connection_config),
                    meta.get("schema"),
                    meta.get("table"),
                    meta.get("custom_query"),
                ),
                ttl=_BASE_TTL_SECONDS,
            ),
        )
    if source == "bigquery":
        sample_kinds = {c.name: c.kind for c in _sample_columns(ctx)}
        assert connection_config is not None
        return BigQueryTableAdapter(
            connection_config,
            database=meta.get("database") or connection_config.get("project_id"),
            schema=meta.get("schema", ""),
            table=meta.get("table"),
            custom_query=meta.get("custom_query"),
            sample_kinds=sample_kinds,
            cache_info=SourceCacheInfo(
                key=(
                    "bigquery",
                    _credential_cache_key(connection_config),
                    meta.get("database") or connection_config.get("project_id"),
                    meta.get("schema"),
                    meta.get("table"),
                    meta.get("custom_query"),
                ),
                ttl=_BASE_TTL_SECONDS,
            ),
        )
    if source == "databricks":
        sample_kinds = {c.name: c.kind for c in _sample_columns(ctx)}
        assert connection_config is not None
        return DatabricksTableAdapter(
            connection_config,
            database=meta.get("database") or connection_config.get("catalog"),
            schema=meta.get("schema", "default"),
            table=meta.get("table"),
            custom_query=meta.get("custom_query"),
            sample_kinds=sample_kinds,
            cache_info=SourceCacheInfo(
                key=(
                    "databricks",
                    _credential_cache_key(connection_config),
                    meta.get("database") or connection_config.get("catalog"),
                    meta.get("schema"),
                    meta.get("table"),
                    meta.get("custom_query"),
                ),
                ttl=_BASE_TTL_SECONDS,
            ),
        )
    # mongodb
    return MongoTableAdapter(
        url,
        database=meta.get("database") or "admin",
        collection=meta.get("collection") or "",
        base_query=meta.get("query") or None,
        columns=_sample_columns(ctx),
        cache_info=SourceCacheInfo(
            key=("mongo", url, meta.get("database"), meta.get("collection")),
            ttl=_BASE_TTL_SECONDS,
        ),
    )

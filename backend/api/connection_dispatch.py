"""Test, discover, and attach helpers for saved connections."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.cloud_datasource_routes import _hydrate_bigquery
from api.cloud_datasource_schemas import (
    BigQueryConnectionParams,
    DatabricksConnectionParams,
    RedshiftConnectionParams,
    SnowflakeConnectionParams,
)
from api.datasource_routes import (
    _connection_kwargs,
    _get_postgres_url_for_test,
    _get_sqlserver_url,
    _mongo_connection_kwargs,
    _sqlserver_connection_kwargs,
)
from api.schemas import MongoConnectionParams, PostgresConnectionParams, SqlServerConnectionParams
from database.connection_store import public_hint
from datasources.bigquery import (
    BigQueryLoadError,
    list_bigquery_datasets,
    list_bigquery_tables,
    resolved_project,
    test_bigquery_connection,
)
from datasources.databricks import (
    DatabricksLoadError,
    list_databricks_schemas,
    list_databricks_tables,
    normalize_scratch_config,
    validate_scratch_access,
)
from datasources.mongodb import (
    MongoLoadError,
    build_mongodb_connection_url,
    list_mongodb_collections,
    list_mongodb_databases,
    test_mongodb_connection,
)
from datasources.postgres import (
    PostgresLoadError,
    list_postgres_schemas,
    list_postgres_tables,
    test_postgres_connection,
)
from datasources.redshift import (
    RedshiftLoadError,
    list_redshift_schemas,
    list_redshift_tables,
    test_redshift_connection,
)
from datasources.snowflake import (
    SnowflakeLoadError,
    list_snowflake_schemas,
    list_snowflake_tables,
    test_snowflake_connection,
)
from datasources.sqlserver import (
    SqlServerLoadError,
    list_sqlserver_schemas,
    list_sqlserver_tables,
    test_sqlserver_connection,
)

logger = logging.getLogger(__name__)

_LABELS = {
    "postgres": "PostgreSQL",
    "sqlserver": "SQL Server",
    "mongodb": "MongoDB",
    "snowflake": "Snowflake",
    "redshift": "Amazon Redshift",
    "bigquery": "BigQuery",
    "databricks": "Databricks",
}


def _enabled(config: dict, connector_id: str) -> bool:
    return bool(config.get("datasources", {}).get(connector_id, {}).get("enabled", True))


def _require_enabled(config: dict, connector_id: str) -> None:
    if not _enabled(config, connector_id):
        raise HTTPException(
            status_code=503, detail=f"{_LABELS[connector_id]} datasource is disabled"
        )


def default_connection_name(connector_id: str, hint: dict[str, Any]) -> str:
    label = _LABELS.get(connector_id, connector_id)
    for key in ("catalog", "database", "project_id", "account", "host", "server_hostname"):
        value = hint.get(key)
        if value:
            return f"{label} · {value}"
    if hint.get("use_default"):
        return f"{label} · default"
    return label


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(exclude_none=True)


def _fail_unless(success: bool, message: str) -> None:
    if not success:
        raise HTTPException(status_code=400, detail=message)


async def test_and_normalize(
    connector_id: str,
    params: dict[str, Any],
    *,
    config: dict,
    user: str,
    db: Session | None,
) -> dict[str, Any]:
    _require_enabled(config, connector_id)
    params = dict(params or {})

    if connector_id == "postgres":
        model = PostgresConnectionParams(**params)
        url = _get_postgres_url_for_test(config, _dump(model) if params else None)
        if not url:
            raise HTTPException(
                status_code=400,
                detail="Provide a PostgreSQL connection or configure a default.",
            )
        success, message = await asyncio.to_thread(test_postgres_connection, url)
        _fail_unless(success, message)
        creds = _dump(model)
        return creds or {"use_default": True}

    if connector_id == "sqlserver":
        model = SqlServerConnectionParams(**params)
        url = _get_sqlserver_url(_sqlserver_connection_kwargs(_dump(model)))
        if not url:
            raise HTTPException(status_code=400, detail="Provide a SQL Server connection.")
        success, message = await asyncio.to_thread(test_sqlserver_connection, url)
        _fail_unless(success, message)
        return _dump(model)

    if connector_id == "mongodb":
        model = MongoConnectionParams(**params)
        dumped = _dump(model)
        kwargs = _mongo_connection_kwargs(dumped)
        if kwargs.get("connection_url"):
            url = kwargs["connection_url"]
        elif kwargs.get("host"):
            url = build_mongodb_connection_url(
                host=kwargs["host"],
                port=kwargs.get("port", 27017),
                # MongoConnectionParams.database selects the database even
                # when credentials use component-style host/port fields.
                database=dumped.get("database") or "admin",
                user=kwargs.get("user") or "",
                password=kwargs.get("password") or "",
            )
        else:
            raise HTTPException(status_code=400, detail="Provide a MongoDB connection.")
        success, message = await asyncio.to_thread(
            test_mongodb_connection, url, dumped.get("database") or "admin"
        )
        _fail_unless(success, message)
        return dumped

    if connector_id == "snowflake":
        model = SnowflakeConnectionParams(**params)
        success, message = await asyncio.to_thread(test_snowflake_connection, _dump(model))
        _fail_unless(success, message)
        return _dump(model)

    if connector_id == "redshift":
        model = RedshiftConnectionParams(**params)
        success, message = await asyncio.to_thread(test_redshift_connection, _dump(model))
        _fail_unless(success, message)
        return _dump(model)

    if connector_id == "bigquery":
        model = BigQueryConnectionParams(**params)
        live = _dump(model)
        if model.auth_mode == "oauth":
            live = _hydrate_bigquery(live, user, db)
        success, message = await asyncio.to_thread(test_bigquery_connection, live)
        _fail_unless(success, message)
        if model.auth_mode == "oauth":
            return {
                "auth_mode": "oauth",
                "project_id": resolved_project(live) or live.get("project_id"),
            }
        live["project_id"] = resolved_project(live) or live.get("project_id")
        return {k: v for k, v in live.items() if v not in (None, "")}

    if connector_id == "databricks":
        model = DatabricksConnectionParams(**params)
        connection = normalize_scratch_config(_dump(model))
        try:
            await asyncio.to_thread(validate_scratch_access, connection)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return connection

    raise HTTPException(status_code=400, detail=f"Unknown connector '{connector_id}'")


async def list_remote(
    connector_id: str,
    credentials: dict[str, Any],
    schema: str,
    *,
    config: dict,
    user: str,
    db: Session | None,
) -> tuple[str, list[dict[str, str]], list[str]]:
    _require_enabled(config, connector_id)
    schema = (schema or "").strip()
    if not schema:
        raise HTTPException(status_code=400, detail="schema is required")
    try:
        if connector_id == "postgres":
            kwargs = _connection_kwargs(credentials)
            if (
                credentials.get("use_default")
                and not kwargs.get("connection_url")
                and not kwargs.get("host")
            ):
                url = (
                    config.get("datasources", {}).get("postgres", {}).get("default_connection_url")
                )
                if url:
                    kwargs["connection_url"] = url
            tables = await asyncio.to_thread(list_postgres_tables, schema=schema, **kwargs)
            return schema, tables, []
        if connector_id == "sqlserver":
            kwargs = _sqlserver_connection_kwargs(credentials)
            tables = await asyncio.to_thread(list_sqlserver_tables, schema=schema, **kwargs)
            return schema, tables, []
        if connector_id == "mongodb":
            kwargs = _mongo_connection_kwargs(credentials)
            raw = await asyncio.to_thread(list_mongodb_collections, database=schema, **kwargs)
            names = [row["collection"] for row in raw]
            tables = [{"schema": schema, "table": name} for name in names]
            return schema, tables, names
        if connector_id == "snowflake":
            tables = await asyncio.to_thread(
                list_snowflake_tables,
                credentials,
                database=credentials.get("database"),
                schema=schema,
            )
            return schema, tables, []
        if connector_id == "redshift":
            tables = await asyncio.to_thread(list_redshift_tables, credentials, schema=schema)
            return schema, tables, []
        if connector_id == "bigquery":
            connection = (
                _hydrate_bigquery(credentials, user, db)
                if credentials.get("auth_mode") == "oauth"
                else credentials
            )
            tables = await asyncio.to_thread(list_bigquery_tables, connection, schema=schema)
            return schema, tables, []
        if connector_id == "databricks":
            tables = await asyncio.to_thread(list_databricks_tables, credentials, schema=schema)
            return schema, tables, []
    except (
        PostgresLoadError,
        SqlServerLoadError,
        MongoLoadError,
        SnowflakeLoadError,
        RedshiftLoadError,
        BigQueryLoadError,
        DatabricksLoadError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=f"Unknown connector '{connector_id}'")


_DEFAULT_NAMESPACES = {
    "postgres": "public",
    "sqlserver": "dbo",
    "mongodb": "admin",
    "snowflake": "PUBLIC",
    "redshift": "public",
    "bigquery": "",
    "databricks": "default",
}


def _preferred_namespace(connector_id: str, credentials: dict[str, Any]) -> str:
    if connector_id == "mongodb":
        return str(credentials.get("database") or "admin").strip()
    if connector_id == "snowflake":
        return str(credentials.get("schema") or "PUBLIC").strip()
    return _DEFAULT_NAMESPACES.get(connector_id, "")


async def list_namespaces(
    connector_id: str,
    credentials: dict[str, Any],
    *,
    config: dict,
    user: str,
    db: Session | None,
) -> tuple[str, list[str], bool, str | None]:
    """Discover browse namespaces without making manual entry unavailable.

    Least-privilege credentials may allow reading a known namespace while
    denying metadata enumeration. In that case the configured/default value is
    returned with ``complete=False`` so the UI remains usable and honest.
    """
    _require_enabled(config, connector_id)
    preferred = _preferred_namespace(connector_id, credentials)
    try:
        if connector_id == "postgres":
            kwargs = _connection_kwargs(credentials)
            if (
                credentials.get("use_default")
                and not kwargs.get("connection_url")
                and not kwargs.get("host")
            ):
                url = (
                    config.get("datasources", {}).get("postgres", {}).get("default_connection_url")
                )
                if url:
                    kwargs["connection_url"] = url
            names = await asyncio.to_thread(list_postgres_schemas, **kwargs)
        elif connector_id == "sqlserver":
            names = await asyncio.to_thread(
                list_sqlserver_schemas, **_sqlserver_connection_kwargs(credentials)
            )
        elif connector_id == "mongodb":
            names = await asyncio.to_thread(
                list_mongodb_databases, **_mongo_connection_kwargs(credentials)
            )
        elif connector_id == "snowflake":
            names = await asyncio.to_thread(
                list_snowflake_schemas,
                credentials,
                database=str(credentials.get("database") or ""),
            )
        elif connector_id == "redshift":
            names = await asyncio.to_thread(list_redshift_schemas, credentials)
        elif connector_id == "bigquery":
            connection = (
                _hydrate_bigquery(credentials, user, db)
                if credentials.get("auth_mode") == "oauth"
                else credentials
            )
            names = await asyncio.to_thread(list_bigquery_datasets, connection)
        elif connector_id == "databricks":
            names = await asyncio.to_thread(list_databricks_schemas, credentials)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown connector '{connector_id}'")
    except (
        PostgresLoadError,
        SqlServerLoadError,
        MongoLoadError,
        SnowflakeLoadError,
        RedshiftLoadError,
        BigQueryLoadError,
        DatabricksLoadError,
    ) as exc:
        logger.info("Namespace discovery unavailable for %s: %s", connector_id, exc)
        fallback = [preferred] if preferred else []
        return (
            preferred,
            fallback,
            False,
            "Available choices could not be enumerated with this connection. "
            "You can still enter one manually.",
        )

    clean = sorted({str(name).strip() for name in names if str(name).strip()})
    if preferred and preferred not in clean:
        clean.insert(0, preferred)
    return preferred, clean, True, None


def hint_for(credentials: dict[str, Any]) -> dict[str, Any]:
    return public_hint(credentials)

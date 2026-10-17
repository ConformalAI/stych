"""Attach a saved-connection table into a chat session."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.cloud_datasource_routes import _add_remote_context, _hydrate_bigquery
from api.connection_dispatch import _require_enabled
from api.datasource_routes import (
    mongodb_load,
    postgres_load,
    sqlserver_load,
)
from api.schemas import (
    MongoConnectionParams,
    MongoLoadRequest,
    PostgresConnectionParams,
    PostgresLoadRequest,
    SqlServerConnectionParams,
    SqlServerLoadRequest,
)
from datasources.bigquery import resolved_project
from datasources.databricks import normalize_scratch_config


async def attach_pin(
    *,
    connector_id: str,
    credentials: dict[str, Any],
    schema: str | None,
    table: str | None,
    query: str | None,
    session_id: Optional[str],
    connection_id: str,
    config: dict,
    user: str,
    db: Session | None,
):
    _require_enabled(config, connector_id)
    if not table and not query:
        raise HTTPException(status_code=400, detail="table_name or custom_query is required")
    schema = schema or ""

    if connector_id == "postgres":
        conn = None if credentials.get("use_default") else PostgresConnectionParams(**credentials)
        return await postgres_load(
            PostgresLoadRequest(
                session_id=session_id,
                connection=conn,
                schema=schema or "public",
                table=table,
                custom_query=query,
                connection_id=connection_id,
            ),
            config,
            user,
        )
    if connector_id == "sqlserver":
        return await sqlserver_load(
            SqlServerLoadRequest(
                session_id=session_id,
                connection=SqlServerConnectionParams(**credentials),
                schema=schema or "dbo",
                table=table,
                custom_query=query,
                connection_id=connection_id,
            ),
            config,
            user,
        )
    if connector_id == "mongodb":
        return await mongodb_load(
            MongoLoadRequest(
                session_id=session_id,
                connection=MongoConnectionParams(**credentials),
                database=schema or credentials.get("database") or "admin",
                collection=table,
                connection_id=connection_id,
            ),
            config,
            user,
        )
    if connector_id == "snowflake":
        schema_default = schema or "PUBLIC"
        return await _add_remote_context(
            source_label=f"{schema_default}.{table or 'query'}",
            connector_id=connector_id,
            connector_label="Snowflake",
            connection_config=credentials,
            schema=schema_default,
            table=table,
            custom_query=query,
            session_id=session_id,
            user=user,
            connection_id=connection_id,
        )
    if connector_id == "redshift":
        schema_default = schema or "public"
        return await _add_remote_context(
            source_label=f"{schema_default}.{table or 'query'}",
            connector_id=connector_id,
            connector_label="Amazon Redshift",
            connection_config=credentials,
            schema=schema_default,
            table=table,
            custom_query=query,
            session_id=session_id,
            user=user,
            connection_id=connection_id,
        )
    if connector_id == "bigquery":
        connection = (
            _hydrate_bigquery(credentials, user, db)
            if credentials.get("auth_mode") == "oauth"
            else dict(credentials)
        )
        connection["project_id"] = resolved_project(connection)
        connection["database"] = connection["project_id"]
        return await _add_remote_context(
            source_label=f"{schema or 'default'}.{table or 'query'}",
            connector_id=connector_id,
            connector_label="BigQuery",
            connection_config=connection,
            schema=schema,
            table=table,
            custom_query=query,
            session_id=session_id,
            user=user,
            connection_id=connection_id,
        )
    if connector_id == "databricks":
        connection = normalize_scratch_config(dict(credentials))
        connection["database"] = connection.get("catalog")
        schema_default = schema or "default"
        return await _add_remote_context(
            source_label=f"{schema_default}.{table or 'query'}",
            connector_id=connector_id,
            connector_label="Databricks",
            connection_config=connection,
            schema=schema_default,
            table=table,
            custom_query=query,
            session_id=session_id,
            user=user,
            connection_id=connection_id,
        )
    raise HTTPException(status_code=400, detail=f"Unknown connector '{connector_id}'")

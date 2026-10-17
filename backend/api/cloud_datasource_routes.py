"""Read-only cloud warehouse connection, discovery, and live-source attachment routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.remote_source import attach_remote_source
from database.connection import get_db
from datasources.bigquery import (
    OAUTH_SERVER_ID as BIGQUERY_OAUTH_SERVER_ID,
)
from datasources.bigquery import (
    BigQueryLoadError,
    is_oauth,
    list_bigquery_datasets,
    list_bigquery_projects,
    list_bigquery_tables,
    resolved_project,
    test_bigquery_connection,
)
from datasources.databricks import (
    DatabricksLoadError,
    list_databricks_tables,
    normalize_scratch_config,
    test_databricks_connection,
)
from datasources.redshift import (
    RedshiftLoadError,
    list_redshift_tables,
    test_redshift_connection,
)
from datasources.snowflake import (
    SnowflakeLoadError,
    list_snowflake_tables,
    test_snowflake_connection,
)
from datasources.warehouse import encode_connection_config

from .cloud_datasource_schemas import (
    BigQueryDatasetsResponse,
    BigQueryLoadRequest,
    BigQueryOAuthStatusResponse,
    BigQueryProjectsResponse,
    BigQueryTablesRequest,
    BigQueryTestRequest,
    DatabricksLoadRequest,
    DatabricksTablesRequest,
    DatabricksTestRequest,
    DatasourceLoadResponse,
    RedshiftLoadRequest,
    RedshiftTablesRequest,
    RedshiftTestRequest,
    SnowflakeLoadRequest,
    SnowflakeTablesRequest,
    SnowflakeTestRequest,
    WarehouseStatusResponse,
    WarehouseTablesResponse,
    WarehouseTestResponse,
)
from .dependencies import get_config, get_current_user

router = APIRouter(prefix="/api/datasources", tags=["datasources"])


def _enabled(config: dict, connector_id: str) -> bool:
    return bool(config.get("datasources", {}).get(connector_id, {}).get("enabled", True))


def _require_enabled(config: dict, connector_id: str, label: str) -> None:
    if not _enabled(config, connector_id):
        raise HTTPException(status_code=503, detail=f"{label} datasource is disabled")


def _connection_dict(model: Any) -> dict[str, Any]:
    return model.model_dump(exclude_none=True)


def _hydrate_bigquery(connection: dict[str, Any], user: str, db: Session | None) -> dict[str, Any]:
    if not is_oauth(connection):
        return connection
    if db is None:
        raise HTTPException(status_code=401, detail="Connect your Google account first.")
    from agents.integration.credential_store import get_credentials  # noqa: PLC0415
    from helpers.oauth_token_refresh import maybe_refresh_oauth_credentials  # noqa: PLC0415

    stored = get_credentials(db, user, BIGQUERY_OAUTH_SERVER_ID) or {}
    stored = maybe_refresh_oauth_credentials(db, user, BIGQUERY_OAUTH_SERVER_ID, stored)
    if not (stored.get("access_token") or stored.get("refresh_token")):
        raise HTTPException(status_code=401, detail="Connect your Google account first.")
    return {
        **connection,
        "auth_mode": "oauth",
        "access_token": stored.get("access_token"),
        "refresh_token": stored.get("refresh_token"),
        "expiry_date": stored.get("expiry_date"),
        "client_id": stored.get("client_id"),
    }


async def _add_remote_context(
    *,
    source_label: str,
    connector_id: str,
    connector_label: str,
    connection_config: dict[str, Any],
    schema: str,
    table: str | None,
    custom_query: str | None,
    session_id: str | None,
    user: str,
    connection_id: str | None = None,
) -> DatasourceLoadResponse:
    try:
        result = await attach_remote_source(
            connector_id=connector_id,
            connector_label=connector_label,
            source_label=source_label,
            schema=schema,
            table=table,
            custom_query=custom_query,
            session_id=session_id,
            user=user,
            connection_id=connection_id,
            metadata={
                "connection_config": encode_connection_config(connection_config),
                "database": (
                    connection_config.get("database")
                    or connection_config.get("project_id")
                    or connection_config.get("catalog")
                ),
                "scratch_catalog": connection_config.get("scratch_catalog"),
                "scratch_schema": connection_config.get("scratch_schema"),
            },
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Session belongs to another user.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DatasourceLoadResponse(**result)


@router.get("/snowflake/status", response_model=WarehouseStatusResponse)
async def snowflake_status(config=Depends(get_config), user: str = Depends(get_current_user)):
    return WarehouseStatusResponse(enabled=_enabled(config, "snowflake"))


@router.post("/snowflake/test", response_model=WarehouseTestResponse)
async def snowflake_test(
    request: SnowflakeTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "snowflake", "Snowflake")
    success, message = await asyncio.to_thread(
        test_snowflake_connection, _connection_dict(request.connection)
    )
    return WarehouseTestResponse(success=success, message=message)


@router.post("/snowflake/tables", response_model=WarehouseTablesResponse)
async def snowflake_tables(
    request: SnowflakeTablesRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "snowflake", "Snowflake")
    connection = _connection_dict(request.connection)
    try:
        tables = await asyncio.to_thread(
            list_snowflake_tables,
            connection,
            database=request.connection.database,
            schema=request.schema,
        )
        return WarehouseTablesResponse(schema=request.schema, tables=tables)
    except SnowflakeLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/snowflake/load", response_model=DatasourceLoadResponse)
async def snowflake_load(
    request: SnowflakeLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "snowflake", "Snowflake")
    connection = _connection_dict(request.connection)
    return await _add_remote_context(
        source_label=f"{request.schema}.{request.table or 'query'}",
        connector_id="snowflake",
        connector_label="Snowflake",
        connection_config=connection,
        schema=request.schema,
        table=request.table,
        custom_query=request.custom_query,
        session_id=request.session_id,
        user=user,
        connection_id=request.connection_id,
    )


@router.get("/redshift/status", response_model=WarehouseStatusResponse)
async def redshift_status(config=Depends(get_config), user: str = Depends(get_current_user)):
    return WarehouseStatusResponse(enabled=_enabled(config, "redshift"))


@router.post("/redshift/test", response_model=WarehouseTestResponse)
async def redshift_test(
    request: RedshiftTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "redshift", "Redshift")
    success, message = await asyncio.to_thread(
        test_redshift_connection, _connection_dict(request.connection)
    )
    return WarehouseTestResponse(success=success, message=message)


@router.post("/redshift/tables", response_model=WarehouseTablesResponse)
async def redshift_tables(
    request: RedshiftTablesRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "redshift", "Redshift")
    connection = _connection_dict(request.connection)
    try:
        tables = await asyncio.to_thread(list_redshift_tables, connection, schema=request.schema)
        return WarehouseTablesResponse(schema=request.schema, tables=tables)
    except RedshiftLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/redshift/load", response_model=DatasourceLoadResponse)
async def redshift_load(
    request: RedshiftLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "redshift", "Redshift")
    connection = _connection_dict(request.connection)
    return await _add_remote_context(
        source_label=f"{request.schema}.{request.table or 'query'}",
        connector_id="redshift",
        connector_label="Amazon Redshift",
        connection_config=connection,
        schema=request.schema,
        table=request.table,
        custom_query=request.custom_query,
        session_id=request.session_id,
        user=user,
        connection_id=request.connection_id,
    )


@router.get("/bigquery/status", response_model=WarehouseStatusResponse)
async def bigquery_status(config=Depends(get_config), user: str = Depends(get_current_user)):
    return WarehouseStatusResponse(enabled=_enabled(config, "bigquery"))


@router.get("/bigquery/oauth/status", response_model=BigQueryOAuthStatusResponse)
async def bigquery_oauth_status(
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from agents.integration.credential_store import get_credentials  # noqa: PLC0415

    stored = get_credentials(db, user, BIGQUERY_OAUTH_SERVER_ID) or {}
    connected = bool(stored.get("access_token") or stored.get("refresh_token"))
    return BigQueryOAuthStatusResponse(connected=connected)


@router.post("/bigquery/test", response_model=WarehouseTestResponse)
async def bigquery_test(
    request: BigQueryTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_enabled(config, "bigquery", "BigQuery")
    connection = _hydrate_bigquery(_connection_dict(request.connection), user, db)
    success, message = await asyncio.to_thread(test_bigquery_connection, connection)
    return WarehouseTestResponse(success=success, message=message)


@router.post("/bigquery/projects", response_model=BigQueryProjectsResponse)
async def bigquery_projects(
    request: BigQueryTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_enabled(config, "bigquery", "BigQuery")
    connection = _hydrate_bigquery(_connection_dict(request.connection), user, db)
    try:
        projects = await asyncio.to_thread(list_bigquery_projects, connection)
        return BigQueryProjectsResponse(projects=projects)
    except BigQueryLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/bigquery/datasets", response_model=BigQueryDatasetsResponse)
async def bigquery_datasets(
    request: BigQueryTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_enabled(config, "bigquery", "BigQuery")
    connection = _hydrate_bigquery(_connection_dict(request.connection), user, db)
    try:
        datasets = await asyncio.to_thread(list_bigquery_datasets, connection)
        return BigQueryDatasetsResponse(datasets=datasets)
    except BigQueryLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/bigquery/tables", response_model=WarehouseTablesResponse)
async def bigquery_tables(
    request: BigQueryTablesRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_enabled(config, "bigquery", "BigQuery")
    connection = _hydrate_bigquery(_connection_dict(request.connection), user, db)
    try:
        tables = await asyncio.to_thread(list_bigquery_tables, connection, schema=request.schema)
        return WarehouseTablesResponse(schema=request.schema, tables=tables)
    except BigQueryLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/bigquery/load", response_model=DatasourceLoadResponse)
async def bigquery_load(
    request: BigQueryLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_enabled(config, "bigquery", "BigQuery")
    connection = _hydrate_bigquery(_connection_dict(request.connection), user, db)
    connection["project_id"] = resolved_project(connection)
    connection["database"] = connection["project_id"]
    return await _add_remote_context(
        source_label=f"{request.schema}.{request.table or 'query'}",
        connector_id="bigquery",
        connector_label="BigQuery",
        connection_config=connection,
        schema=request.schema,
        table=request.table,
        custom_query=request.custom_query,
        session_id=request.session_id,
        user=user,
        connection_id=request.connection_id,
    )


@router.get("/databricks/status", response_model=WarehouseStatusResponse)
async def databricks_status(config=Depends(get_config), user: str = Depends(get_current_user)):
    return WarehouseStatusResponse(enabled=_enabled(config, "databricks"))


@router.post("/databricks/test", response_model=WarehouseTestResponse)
async def databricks_test(
    request: DatabricksTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "databricks", "Databricks")
    success, message = await asyncio.to_thread(
        test_databricks_connection, _connection_dict(request.connection)
    )
    return WarehouseTestResponse(success=success, message=message)


@router.post("/databricks/tables", response_model=WarehouseTablesResponse)
async def databricks_tables(
    request: DatabricksTablesRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "databricks", "Databricks")
    connection = _connection_dict(request.connection)
    try:
        tables = await asyncio.to_thread(list_databricks_tables, connection, schema=request.schema)
        return WarehouseTablesResponse(schema=request.schema, tables=tables)
    except DatabricksLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/databricks/load", response_model=DatasourceLoadResponse)
async def databricks_load(
    request: DatabricksLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    _require_enabled(config, "databricks", "Databricks")
    connection = normalize_scratch_config(_connection_dict(request.connection))
    connection["database"] = connection.get("catalog")
    return await _add_remote_context(
        source_label=f"{request.schema}.{request.table or 'query'}",
        connector_id="databricks",
        connector_label="Databricks",
        connection_config=connection,
        schema=request.schema,
        table=request.table,
        custom_query=request.custom_query,
        session_id=request.session_id,
        user=user,
        connection_id=request.connection_id,
    )

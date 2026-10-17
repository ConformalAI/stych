"""Attach external databases as live, source-native session datasets."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from api.remote_source import attach_remote_source
from datasources.mongodb import (
    MongoLoadError,
    build_mongodb_connection_url,
    list_mongodb_collections,
)
from datasources.postgres import (
    PostgresLoadError,
    build_postgres_connection_url,
    list_postgres_tables,
    test_postgres_connection,
)
from datasources.sqlserver import (
    SqlServerLoadError,
    build_sqlserver_connection_url,
    list_sqlserver_tables,
    test_sqlserver_connection,
)

from .dependencies import get_config, get_current_user
from .schemas import (
    DatasourceLoadResponse,
    MongoCollectionsRequest,
    MongoCollectionsResponse,
    MongoLoadRequest,
    MongoStatusResponse,
    MongoTestRequest,
    MongoTestResponse,
    PostgresLoadRequest,
    PostgresStatusResponse,
    PostgresTablesRequest,
    PostgresTablesResponse,
    PostgresTestRequest,
    PostgresTestResponse,
    SqlServerLoadRequest,
    SqlServerStatusResponse,
    SqlServerTablesRequest,
    SqlServerTablesResponse,
    SqlServerTestRequest,
    SqlServerTestResponse,
)

router = APIRouter(prefix="/api/datasources", tags=["datasources"])


def _connection_kwargs(conn: Optional[dict]) -> dict:
    """Build kwargs for postgres from request connection or config default URL."""
    if not conn:
        return {}
    return {
        "connection_url": conn.get("connection_url"),
        "host": conn.get("host"),
        "port": conn.get("port"),
        "database": conn.get("database"),
        "user": conn.get("user") or "",
        "password": conn.get("password") or "",
        "sslmode": conn.get("sslmode"),
    }


def _get_postgres_url_for_test(config: dict, conn: Optional[dict]) -> Optional[str]:
    """Resolve Postgres URL from config default or connection params."""
    kwargs = _connection_kwargs(conn)
    if kwargs.get("connection_url"):
        return kwargs["connection_url"]
    if kwargs.get("host"):
        return build_postgres_connection_url(
            host=kwargs["host"],
            port=kwargs.get("port", 5432),
            database=kwargs.get("database", "postgres"),
            user=kwargs.get("user", ""),
            password=kwargs.get("password", ""),
            sslmode=kwargs.get("sslmode"),
        )
    url = config.get("datasources", {}).get("postgres", {}).get("default_connection_url")
    return url if url else None


@router.get("/postgres/status", response_model=PostgresStatusResponse)
async def postgres_status(
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Return whether a default Postgres connection URL is configured (for UI)."""
    if not config.get("datasources", {}).get("postgres", {}).get("enabled", True):
        return PostgresStatusResponse(default_connection_available=False)
    url = config.get("datasources", {}).get("postgres", {}).get("default_connection_url")
    return PostgresStatusResponse(default_connection_available=bool(url and str(url).strip()))


@router.post("/postgres/test", response_model=PostgresTestResponse)
async def postgres_test(
    request: PostgresTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Test PostgreSQL connection. Use default from config if no connection in body."""
    if not config.get("datasources", {}).get("postgres", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="PostgreSQL datasource is disabled")
    conn = request.connection.model_dump() if request.connection else None
    url = _get_postgres_url_for_test(config, conn)
    if not url:
        raise HTTPException(
            status_code=400,
            detail="Provide connection in body or set POSTGRES_URL / datasources.postgres.default_connection_url",
        )
    ok, message = test_postgres_connection(url)
    return PostgresTestResponse(success=ok, message=message)


@router.post("/postgres/tables", response_model=PostgresTablesResponse)
async def postgres_list_tables(
    request: PostgresTablesRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """List tables in a PostgreSQL schema."""
    if not config.get("datasources", {}).get("postgres", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="PostgreSQL datasource is disabled")
    kwargs = _connection_kwargs(request.connection.model_dump() if request.connection else None)
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        url = config.get("datasources", {}).get("postgres", {}).get("default_connection_url")
        if url:
            kwargs["connection_url"] = url
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        raise HTTPException(
            status_code=400,
            detail="Provide connection in request body or set POSTGRES_URL / datasources.postgres.default_connection_url",
        )
    try:
        tables = list_postgres_tables(schema=request.schema, **kwargs)
        return PostgresTablesResponse(schema=request.schema, tables=tables)
    except PostgresLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/postgres/load", response_model=DatasourceLoadResponse)
async def postgres_load(
    request: PostgresLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Load a PostgreSQL table/custom query into the session as another dataset."""
    if not config.get("datasources", {}).get("postgres", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="PostgreSQL datasource is disabled")
    kwargs = _connection_kwargs(request.connection.model_dump() if request.connection else None)
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        url = config.get("datasources", {}).get("postgres", {}).get("default_connection_url")
        if url:
            kwargs["connection_url"] = url
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        raise HTTPException(
            status_code=400,
            detail="Provide connection in request body or set POSTGRES_URL / datasources.postgres.default_connection_url",
        )
    try:
        connection_url = _get_postgres_url_for_test(
            config, request.connection.model_dump() if request.connection else None
        )
        result = await attach_remote_source(
            connector_id="postgres",
            connector_label="PostgreSQL",
            source_label=f"{request.schema}.{request.table or 'query'}",
            schema=request.schema,
            table=request.table,
            custom_query=request.custom_query,
            session_id=request.session_id,
            user=user,
            connection_id=request.connection_id,
            metadata={"connection_url": connection_url},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DatasourceLoadResponse(**result)


def _sqlserver_connection_kwargs(conn: Optional[dict]) -> dict:
    """Build kwargs for SQL Server from request connection."""
    if not conn:
        return {}
    return {
        "connection_url": conn.get("connection_url"),
        "host": conn.get("host"),
        "port": conn.get("port", 1433),
        "database": conn.get("database", "master"),
        "user": conn.get("user") or "",
        "password": conn.get("password") or "",
    }


def _get_sqlserver_url(conn_kwargs: dict) -> Optional[str]:
    """Resolve SQL Server URL from kwargs."""
    if conn_kwargs.get("connection_url"):
        return conn_kwargs["connection_url"]
    if conn_kwargs.get("host"):
        return build_sqlserver_connection_url(
            host=conn_kwargs["host"],
            port=conn_kwargs.get("port", 1433),
            database=conn_kwargs.get("database", "master"),
            user=conn_kwargs.get("user", ""),
            password=conn_kwargs.get("password", ""),
        )
    return None


@router.get("/sqlserver/status", response_model=SqlServerStatusResponse)
async def sqlserver_status(
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Return whether SQL Server datasource is enabled."""
    enabled = config.get("datasources", {}).get("sqlserver", {}).get("enabled", True)
    return SqlServerStatusResponse(enabled=bool(enabled))


@router.post("/sqlserver/test", response_model=SqlServerTestResponse)
async def sqlserver_test(
    request: SqlServerTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Test SQL Server connection."""
    if not config.get("datasources", {}).get("sqlserver", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="SQL Server datasource is disabled")
    conn = request.connection.model_dump() if request.connection else None
    kwargs = _sqlserver_connection_kwargs(conn)
    url = _get_sqlserver_url(kwargs)
    if not url:
        raise HTTPException(
            status_code=400,
            detail="Provide connection details (host or connection_url) in request body.",
        )
    ok, message = test_sqlserver_connection(url)
    return SqlServerTestResponse(success=ok, message=message)


@router.post("/sqlserver/tables", response_model=SqlServerTablesResponse)
async def sqlserver_list_tables(
    request: SqlServerTablesRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """List tables in a SQL Server schema."""
    if not config.get("datasources", {}).get("sqlserver", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="SQL Server datasource is disabled")
    conn = request.connection.model_dump() if request.connection else None
    kwargs = _sqlserver_connection_kwargs(conn)
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        raise HTTPException(
            status_code=400,
            detail="Provide connection details (host or connection_url) in request body.",
        )
    try:
        tables = list_sqlserver_tables(schema=request.schema, **kwargs)
        return SqlServerTablesResponse(schema=request.schema, tables=tables)
    except SqlServerLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sqlserver/load", response_model=DatasourceLoadResponse)
async def sqlserver_load(
    request: SqlServerLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Load a SQL Server table/query into the session as a dataset."""
    if not config.get("datasources", {}).get("sqlserver", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="SQL Server datasource is disabled")
    conn = request.connection.model_dump() if request.connection else None
    kwargs = _sqlserver_connection_kwargs(conn)
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        raise HTTPException(
            status_code=400,
            detail="Provide connection details (host or connection_url) in request body.",
        )
    try:
        result = await attach_remote_source(
            connector_id="sqlserver",
            connector_label="SQL Server",
            source_label=f"{request.schema}.{request.table or 'query'}",
            schema=request.schema,
            table=request.table,
            custom_query=request.custom_query,
            session_id=request.session_id,
            user=user,
            connection_id=request.connection_id,
            metadata={"connection_url": _get_sqlserver_url(kwargs)},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DatasourceLoadResponse(**result)


# ----- MongoDB -----


def _mongo_connection_kwargs(conn: Optional[dict]) -> dict:
    """Build kwargs for MongoDB from request connection."""
    if not conn:
        return {}
    return {
        "connection_url": conn.get("connection_url"),
        "host": conn.get("host"),
        "port": conn.get("port", 27017),
        "user": conn.get("user") or "",
        "password": conn.get("password") or "",
    }


@router.get("/mongodb/status", response_model=MongoStatusResponse)
async def mongodb_status(
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Return whether MongoDB datasource is enabled."""
    enabled = config.get("datasources", {}).get("mongodb", {}).get("enabled", True)
    return MongoStatusResponse(enabled=bool(enabled))


@router.post("/mongodb/test", response_model=MongoTestResponse)
async def mongodb_test(
    request: MongoTestRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Test MongoDB connection."""
    if not config.get("datasources", {}).get("mongodb", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="MongoDB datasource is disabled")
    from datasources.mongodb import build_mongodb_connection_url, test_mongodb_connection

    conn = request.connection.model_dump() if request.connection else None
    kwargs = _mongo_connection_kwargs(conn)
    if kwargs.get("connection_url"):
        url = kwargs["connection_url"]
    elif kwargs.get("host"):
        url = build_mongodb_connection_url(
            host=kwargs["host"],
            port=kwargs.get("port", 27017),
            database=request.database,
            user=kwargs.get("user", ""),
            password=kwargs.get("password", ""),
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide connection details (host or connection_url) in request body.",
        )
    ok, message = test_mongodb_connection(url, request.database)
    return MongoTestResponse(success=ok, message=message)


@router.post("/mongodb/collections", response_model=MongoCollectionsResponse)
async def mongodb_list_collections(
    request: MongoCollectionsRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """List collections in a MongoDB database."""
    if not config.get("datasources", {}).get("mongodb", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="MongoDB datasource is disabled")
    conn = request.connection.model_dump() if request.connection else None
    kwargs = _mongo_connection_kwargs(conn)
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        raise HTTPException(
            status_code=400,
            detail="Provide connection details (host or connection_url) in request body.",
        )
    try:
        collections = list_mongodb_collections(database=request.database, **kwargs)
        return MongoCollectionsResponse(
            database=request.database,
            collections=[c["collection"] for c in collections],
        )
    except MongoLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/mongodb/load", response_model=DatasourceLoadResponse)
async def mongodb_load(
    request: MongoLoadRequest,
    config=Depends(get_config),
    user: str = Depends(get_current_user),
):
    """Load a MongoDB collection into the session as a dataset."""
    if not config.get("datasources", {}).get("mongodb", {}).get("enabled", True):
        raise HTTPException(status_code=503, detail="MongoDB datasource is disabled")
    conn = request.connection.model_dump() if request.connection else None
    kwargs = _mongo_connection_kwargs(conn)
    if not kwargs.get("connection_url") and not kwargs.get("host"):
        raise HTTPException(
            status_code=400,
            detail="Provide connection details (host or connection_url) in request body.",
        )
    if not request.collection:
        raise HTTPException(status_code=400, detail="A collection name is required.")
    try:
        connection_url = kwargs.get("connection_url") or build_mongodb_connection_url(
            host=kwargs.get("host") or "localhost",
            port=kwargs.get("port", 27017),
            database=request.database,
            user=kwargs.get("user") or "",
            password=kwargs.get("password") or "",
        )
        result = await attach_remote_source(
            connector_id="mongodb",
            connector_label="MongoDB",
            source_label=f"{request.database}.{request.collection}",
            schema=request.database,
            table=request.collection,
            custom_query=None,
            session_id=request.session_id,
            user=user,
            connection_id=request.connection_id,
            metadata={
                "connection_url": connection_url,
                "database": request.database,
                "collection": request.collection,
                "query": request.query,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DatasourceLoadResponse(**result)

"""
SQL Server data source connector.

Loads a table or custom query result into a pandas DataFrame for analysis.
Uses SQLAlchemy with the pymssql dialect (mssql+pymssql://).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd
import sqlparse
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlparse import tokens as T

logger = logging.getLogger(__name__)


class SqlServerLoadError(Exception):
    """Raised when loading from SQL Server fails."""

    pass


def build_sqlserver_connection_url(
    host: str,
    port: int = 1433,
    database: str = "master",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> str:
    """Build a SQLAlchemy SQL Server URL using pymssql dialect."""
    u = quote_plus(user or "")
    p = quote_plus(password or "")
    user_part = f"{u}:{p}" if p else u
    auth = f"{user_part}@" if user_part else ""
    return f"mssql+pymssql://{auth}{host}:{port}/{database}"


def _get_engine(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 1433,
    database: str = "master",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> Engine:
    """Create a SQLAlchemy engine from URL or from components."""
    connect_args: Dict[str, Any] = {"login_timeout": 10, "timeout": 30}
    if connection_url:
        return create_engine(connection_url, pool_pre_ping=True, connect_args=connect_args)
    url = build_sqlserver_connection_url(
        host=host or "localhost",
        port=port,
        database=database,
        user=user or "",
        password=password or "",
    )
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def test_sqlserver_connection(connection_url: str) -> Tuple[bool, str]:
    """
    Test connectivity to SQL Server with the given URL.

    Returns:
        (success, message)
    """
    if not connection_url or not connection_url.strip():
        return False, "No connection URL provided"
    engine = None
    try:
        engine = _get_engine(connection_url=connection_url.strip())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Connection successful"
    except Exception:  # noqa: BLE001 — public connection tests return a safe message
        return False, "Connection failed. Verify the server, credentials, database, and network."
    finally:
        if engine is not None:
            engine.dispose()


def list_sqlserver_tables(
    schema: str = "dbo",
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 1433,
    database: str = "master",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> List[Dict[str, str]]:
    """
    List tables in the given schema.

    Returns:
        List of {"schema": str, "table": str}
    """
    engine = _get_engine(
        connection_url=connection_url,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        **kwargs,
    )
    try:
        insp = inspect(engine)
        tables = insp.get_table_names(schema=schema)
        return [{"schema": schema, "table": t} for t in tables]
    except SQLAlchemyError as e:
        logger.error("Failed to list SQL Server tables: %s", e)
        raise SqlServerLoadError(str(e)) from e
    finally:
        engine.dispose()


def list_sqlserver_schemas(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 1433,
    database: str = "master",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> List[str]:
    """List user-facing schemas in the connected SQL Server database."""
    engine = _get_engine(
        connection_url=connection_url,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        **kwargs,
    )
    try:
        names = inspect(engine).get_schema_names()
        return sorted(name for name in names if name.lower() not in {"information_schema", "sys"})
    except SQLAlchemyError as e:
        logger.error("Failed to list SQL Server schemas: %s", e)
        raise SqlServerLoadError(str(e)) from e
    finally:
        engine.dispose()


_FORBIDDEN_QUERY_TOKENS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "EXECUTE",
    "EXEC",
    "CALL",
    "COPY",
    "MERGE",
    "INTO",
    "OPENQUERY",
    "OPENROWSET",
    "OPENDATASOURCE",
    "SP_EXECUTESQL",
}


def validate_custom_query(query: str) -> str:
    """Return one read-only SELECT/CTE query, rejecting write tokens."""
    stripped = (query or "").strip()
    if ";" in stripped:
        raise SqlServerLoadError(
            "custom_query must be a single statement — semicolons are not allowed."
        )
    statements = [stmt for stmt in sqlparse.parse(stripped) if str(stmt).strip()]
    if len(statements) != 1 or statements[0].get_type() != "SELECT":
        raise SqlServerLoadError("custom_query must be a SELECT or WITH statement.")
    for token in statements[0].flatten():
        ident = (token.normalized or token.value or "").strip('[]`"() ').upper()
        if ident in _FORBIDDEN_QUERY_TOKENS:
            raise SqlServerLoadError(
                f"custom_query contains disallowed keyword '{ident}'. "
                "Only read-only SELECT statements are permitted."
            )
        if token.ttype in T.Keyword or token.ttype in T.Keyword.DML or token.ttype in T.Keyword.DDL:
            keyword = token.normalized.upper()
            if keyword in _FORBIDDEN_QUERY_TOKENS:
                raise SqlServerLoadError(
                    f"custom_query contains disallowed keyword '{keyword}'. "
                    "Only read-only SELECT statements are permitted."
                )
    return stripped


def load_from_sqlserver(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 1433,
    database: str = "master",
    user: str = "",
    password: str = "",
    schema: str = "dbo",
    table: Optional[str] = None,
    custom_query: Optional[str] = None,
    max_rows: int = 0,
    preview_rows: int = 10_000,
    **kwargs: Any,
) -> Tuple[pd.DataFrame, str]:
    """
    Load a preview sample from SQL Server into a pandas DataFrame.

    Either provide (schema + table) or custom_query, not both.

    Returns:
        (DataFrame, source_label)
    """
    if table and custom_query:
        raise SqlServerLoadError("Provide either table or custom_query, not both.")
    if not table and not custom_query:
        raise SqlServerLoadError("Provide either table or custom_query.")

    engine = _get_engine(
        connection_url=connection_url,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        **kwargs,
    )

    try:
        if custom_query:
            query = validate_custom_query(custom_query)

            # SQL Server uses TOP instead of LIMIT
            preview_query = f"SELECT TOP {preview_rows} * FROM ({query}) AS _src"
            with engine.connect() as conn:
                df = pd.read_sql(text(preview_query), conn, coerce_float=True)
            source_label = "Custom query"
        else:
            # Escape internal ] to prevent bracket-identifier injection (] → ]]).
            safe_schema = (schema or "dbo").replace("]", "]]")
            safe_table = table.replace("]", "]]")
            quoted_schema = f"[{safe_schema}]"
            quoted_table = f"[{safe_table}]"
            full_name = f"{schema or 'dbo'}.{table}"
            preview_query = f"SELECT TOP {preview_rows} * FROM {quoted_schema}.{quoted_table}"
            with engine.connect() as conn:
                df = pd.read_sql(text(preview_query), conn, coerce_float=True)
            source_label = full_name

        if df.empty:
            logger.warning("SQL Server preview returned 0 rows")
        else:
            logger.info(
                "Loaded preview of %s rows from SQL Server: %s",
                len(df),
                source_label,
            )
        return df, source_label
    except SqlServerLoadError:
        raise
    except SQLAlchemyError as e:
        logger.error("SQL Server load failed: %s", e)
        raise SqlServerLoadError(str(e)) from e
    finally:
        engine.dispose()

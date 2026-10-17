"""
PostgreSQL data source connector.

Loads table or custom query result into a pandas DataFrame for analysis.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


class PostgresLoadError(Exception):
    """Raised when loading from PostgreSQL fails."""

    pass


def build_postgres_connection_url(
    host: str,
    port: int = 5432,
    database: str = "postgres",
    user: str = "",
    password: str = "",
    sslmode: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """Build a SQLAlchemy PostgreSQL URL."""
    from urllib.parse import quote_plus

    user_part = ""
    if user or password:
        u = quote_plus(user or "")
        p = quote_plus(password or "")
        user_part = f"{u}:{p}" if p else u
    auth = f"{user_part}@" if user_part else ""
    opts = []
    if sslmode:
        opts.append(f"sslmode={sslmode}")
    query = "&".join(opts) if opts else None
    path = f"/{database}"
    q = f"?{query}" if query else ""
    return f"postgresql+psycopg2://{auth}{host}:{port}{path}{q}"


def _get_engine(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 5432,
    database: str = "postgres",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> Engine:
    """Create a SQLAlchemy engine from URL or from components."""
    connect_args = {"connect_timeout": 10}  # Fail fast on unreachable hosts
    if connection_url:
        return create_engine(connection_url, pool_pre_ping=True, connect_args=connect_args)
    url = build_postgres_connection_url(
        host=host or "localhost",
        port=port,
        database=database,
        user=user or "",
        password=password or "",
        **kwargs,
    )
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def test_postgres_connection(connection_url: str) -> Tuple[bool, str]:
    """
    Test connectivity to PostgreSQL with the given URL.

    Returns:
        (success, message)
    """
    if not connection_url or not connection_url.strip():
        return False, "No connection URL provided"
    engine = create_engine(connection_url.strip(), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Connection successful"
    except SQLAlchemyError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    finally:
        engine.dispose()


def list_postgres_tables(
    schema: str = "public",
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 5432,
    database: str = "postgres",
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
        logger.error("Failed to list Postgres tables: %s", e)
        raise PostgresLoadError(str(e)) from e
    finally:
        engine.dispose()


def list_postgres_schemas(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 5432,
    database: str = "postgres",
    user: str = "",
    password: str = "",
    **kwargs: Any,
) -> List[str]:
    """List user-facing schemas in the connected PostgreSQL database."""
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
        return sorted(
            name for name in names if name != "information_schema" and not name.startswith("pg_")
        )
    except SQLAlchemyError as e:
        logger.error("Failed to list Postgres schemas: %s", e)
        raise PostgresLoadError(str(e)) from e
    finally:
        engine.dispose()


def load_from_postgres(
    connection_url: Optional[str] = None,
    host: Optional[str] = None,
    port: int = 5432,
    database: str = "postgres",
    user: str = "",
    password: str = "",
    schema: str = "public",
    table: Optional[str] = None,
    custom_query: Optional[str] = None,
    max_rows: int = 0,
    preview_rows: int = 10_000,
    **kwargs: Any,
) -> Tuple[pd.DataFrame, str]:
    """
    Load a *preview* sample from PostgreSQL into a pandas DataFrame.

    The preview is used for LLM context (column profiles, sample values) and
    immediate UI feedback.  The full dataset is accessed server-side via a
    temporary table created lazily by the SQL agent (see ensure_temp_table).

    Either provide (schema + table) or custom_query, not both.

    Args:
        connection_url: Full postgresql+psycopg2:// URL.
        host, port, database, user, password: Connection params if not using URL.
        schema: Schema name (default public).
        table: Table name (required if custom_query not set).
        custom_query: Raw SQL SELECT. Must be a read-only SELECT query.
        max_rows: Working-copy cap. Zero means the complete attached source.
        preview_rows: Max rows to fetch into memory for in-process use (default 10 000).

    Returns:
        (DataFrame, source_label) e.g. (df, "public.users")
    """
    if table and custom_query:
        raise PostgresLoadError("Provide either table or custom_query, not both.")
    if not table and not custom_query:
        raise PostgresLoadError("Provide either table or custom_query.")

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
            query = custom_query.strip()
            query_upper = query.upper()

            if not query_upper.lstrip().startswith("SELECT"):
                raise PostgresLoadError("custom_query must be a SELECT statement.")

            # Block multi-statement injection via semicolons.
            if ";" in query:
                raise PostgresLoadError(
                    "custom_query must be a single statement — semicolons are not allowed."
                )

            # Reject write/DDL keywords that could appear after a valid SELECT prefix.
            _FORBIDDEN_KEYWORDS = (
                "INSERT ",
                "UPDATE ",
                "DELETE ",
                "DROP ",
                "CREATE ",
                "ALTER ",
                "TRUNCATE ",
                "GRANT ",
                "REVOKE ",
                "EXECUTE ",
                "EXEC ",
                "CALL ",
                "COPY ",
                "MERGE ",
                "SELECT INTO ",
            )
            for kw in _FORBIDDEN_KEYWORDS:
                if kw in query_upper:
                    raise PostgresLoadError(
                        f"custom_query contains disallowed keyword '{kw.strip()}'. "
                        "Only read-only SELECT statements are permitted."
                    )

            # Wrap the user query so we can apply the preview limit safely.
            preview_query = f"SELECT * FROM ({query}) AS _src LIMIT {preview_rows}"
            with engine.connect() as conn:
                df = pd.read_sql(text(preview_query), conn, coerce_float=True)
            source_label = "Custom query"
        else:
            # Escape internal double-quotes to prevent identifier injection.
            safe_schema = (schema or "public").replace('"', '""')
            safe_table = table.replace('"', '""')
            quoted_schema = f'"{safe_schema}"'
            quoted_table = f'"{safe_table}"'
            full_name = f"{schema or 'public'}.{table}"
            preview_query = f"SELECT * FROM {quoted_schema}.{quoted_table} LIMIT {preview_rows}"
            with engine.connect() as conn:
                df = pd.read_sql(text(preview_query), conn, coerce_float=True)
            source_label = full_name

        if df.empty:
            logger.warning("PostgreSQL preview returned 0 rows")
        else:
            logger.info(
                "Loaded preview %s/%s rows from PostgreSQL: %s",
                len(df),
                max_rows,
                source_label,
            )
        return df, source_label
    except PostgresLoadError:
        raise
    except SQLAlchemyError as e:
        logger.error("PostgreSQL load failed: %s", e)
        raise PostgresLoadError(str(e)) from e
    finally:
        engine.dispose()

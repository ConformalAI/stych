"""Read-only Amazon Redshift source edge using IAM access-key authentication."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from typing import Any, Optional

import pandas as pd

from .dbapi import borrowed
from .warehouse import WarehouseError, quote_ident, relation_ref, validate_read_only_query

logger = logging.getLogger(__name__)

_PROVISIONED_HOST = re.compile(
    r"^([^.]+)\.[^.]+\.([^.]+)\.redshift(?:-dev)?\.amazonaws\.com$",
    re.IGNORECASE,
)
_SERVERLESS_HOST = re.compile(
    r"^([^.]+)\.([^.]+)\.([^.]+)\.redshift-serverless(?:-dev)?\.amazonaws\.com$",
    re.IGNORECASE,
)


class RedshiftLoadError(WarehouseError):
    """Raised when Redshift discovery or extraction fails."""


def connect_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build redshift_connector.connect kwargs. Never forwards a database password."""
    host = str(config.get("host") or "").strip()
    database = str(config.get("database") or "").strip()
    db_user = str(config.get("user") or config.get("db_user") or "").strip()
    access_key_id = str(config.get("access_key_id") or "").strip()
    secret_access_key = str(config.get("secret_access_key") or "").strip()
    if not all((host, database, db_user, access_key_id, secret_access_key)):
        raise RedshiftLoadError(
            "Redshift IAM authentication requires host, database, user, "
            "access_key_id, and secret_access_key."
        )

    kwargs: dict[str, Any] = {
        "iam": True,
        "ssl": True,
        "host": host,
        "port": int(config.get("port") or 5439),
        "database": database,
        "db_user": db_user,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "application_name": "stych",
        "timeout": 10,
    }
    for key in ("session_token", "region", "cluster_identifier"):
        value = config.get(key)
        if value not in (None, ""):
            kwargs[key] = value

    serverless = _SERVERLESS_HOST.match(host)
    provisioned = _PROVISIONED_HOST.match(host)
    if serverless:
        kwargs["is_serverless"] = True
        kwargs.setdefault("serverless_work_group", serverless.group(1))
        kwargs.setdefault("serverless_acct_id", serverless.group(2))
        kwargs.setdefault("region", serverless.group(3))
        kwargs.pop("cluster_identifier", None)
    elif provisioned:
        kwargs.setdefault("cluster_identifier", provisioned.group(1))
        kwargs.setdefault("region", provisioned.group(2))
    elif "cluster_identifier" not in kwargs:
        raise RedshiftLoadError(
            "cluster_identifier is required when the host is not a standard "
            "Redshift or Redshift Serverless endpoint."
        )
    return kwargs


def _connect(config: Mapping[str, Any]):
    try:
        import redshift_connector  # noqa: PLC0415
    except ImportError as exc:
        raise RedshiftLoadError(
            "Redshift support is not installed. Install the backend connector dependencies."
        ) from exc
    return redshift_connector.connect(**connect_kwargs(config))


def open_connection(config: Mapping[str, Any]):
    """Open one Redshift session. Callers must close it."""
    return _connect(config)


def run_redshift_query(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    timeout_seconds: int = 8,
    connection=None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    try:
        with borrowed(connection, lambda: _connect(config)) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SET statement_timeout TO {int(timeout_seconds * 1000)}")
                cursor.execute(statement, parameters)
                names = [str(item[0]) for item in (cursor.description or [])]
                return names, [tuple(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
    except RedshiftLoadError:
        raise
    except Exception as exc:
        raise RedshiftLoadError(str(exc)) from exc


def execute_redshift_statement(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    timeout_seconds: int = 600,
    connection=None,
) -> None:
    """Run one DDL/DML statement. Does not fetch rows."""
    try:
        with borrowed(connection, lambda: _connect(config)) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SET statement_timeout TO {int(timeout_seconds * 1000)}")
                cursor.execute(statement, parameters)
                conn.commit()
            finally:
                cursor.close()
    except RedshiftLoadError:
        raise
    except Exception as exc:
        raise RedshiftLoadError(str(exc)) from exc


def stream_redshift_query(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    batch_rows: int = 50_000,
    timeout_seconds: int = 1_800,
) -> "Iterator[tuple[list[str], list[tuple[Any, ...]]]]":
    """Yield ``(column_names, rows)`` chunks for one read statement.

    One execute fetched incrementally, so the result is a single query snapshot
    and never lands in memory whole. Callers must drain or close the generator.
    """
    try:
        with borrowed(None, lambda: _connect(config)) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SET statement_timeout TO {int(timeout_seconds * 1000)}")
                cursor.execute(statement, parameters)
                names = [str(item[0]) for item in (cursor.description or [])]
                while True:
                    rows = cursor.fetchmany(int(batch_rows))
                    if not rows:
                        return
                    yield names, [tuple(row) for row in rows]
            finally:
                cursor.close()
    except RedshiftLoadError:
        raise
    except Exception as exc:
        raise RedshiftLoadError(str(exc)) from exc


def test_redshift_connection(config: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        run_redshift_query(config, "SELECT 1")
        return True, "Connection successful"
    except RedshiftLoadError as exc:
        return False, str(exc)


def list_redshift_tables(
    config: Mapping[str, Any], *, schema: str, limit: int = 1_000
) -> list[dict[str, str]]:
    database = str(config.get("database") or "")
    if not database:
        raise RedshiftLoadError("Redshift database is required for table discovery.")
    statement = (
        f"SHOW TABLES FROM SCHEMA {quote_ident(database)}.{quote_ident(schema)} "
        f"LIMIT {int(limit)}"
    )
    names, rows = run_redshift_query(config, statement)
    lowered = [name.lower() for name in names]
    try:
        schema_index = lowered.index("schema_name")
        table_index = lowered.index("table_name")
    except ValueError as exc:
        raise RedshiftLoadError("Redshift returned an unexpected SHOW TABLES result.") from exc
    return [{"schema": str(row[schema_index]), "table": str(row[table_index])} for row in rows]


def list_redshift_schemas(config: Mapping[str, Any], *, limit: int = 1_000) -> list[str]:
    """List user-facing schemas in the connected Redshift database."""
    statement = (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name <> 'information_schema' "
        "AND schema_name NOT LIKE 'pg_%' "
        f"ORDER BY schema_name LIMIT {int(limit)}"
    )
    _, rows = run_redshift_query(config, statement)
    return sorted(str(row[0]) for row in rows)


def load_from_redshift(
    config: Mapping[str, Any],
    *,
    schema: str,
    table: Optional[str] = None,
    custom_query: Optional[str] = None,
    preview_rows: int = 10_000,
) -> tuple[pd.DataFrame, str]:
    if bool(table) == bool(custom_query):
        raise RedshiftLoadError("Provide either table or custom_query, not both.")
    if custom_query:
        try:
            query = validate_read_only_query(custom_query)
        except WarehouseError as exc:
            raise RedshiftLoadError(str(exc)) from exc
        statement = f"SELECT * FROM ({query}) AS _src LIMIT {int(preview_rows)}"
        label = "Custom query"
    else:
        statement = (
            f"SELECT * FROM {relation_ref(database=None, schema=schema, table=str(table))} "
            f"LIMIT {int(preview_rows)}"
        )
        label = f"{schema}.{table}"

    connection = None
    cursor = None
    try:
        connection = _connect(config)
        cursor = connection.cursor()
        cursor.execute("SET statement_timeout TO 60000")
        cursor.execute(statement)
        names = [str(item[0]) for item in (cursor.description or [])]
        rows = cursor.fetchall()
        return pd.DataFrame.from_records(rows, columns=names), label
    except RedshiftLoadError:
        raise
    except Exception as exc:
        logger.error("Redshift load failed: %s", exc)
        raise RedshiftLoadError(str(exc)) from exc
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()

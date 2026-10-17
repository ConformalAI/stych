"""Databricks SQL source edge using a personal access token.

Reads the customer's table live and clones it into a Stych-owned scratch schema
(default ``{catalog}.stych_temp``) for working-copy writes. The source table is
never mutated.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterator, Mapping
from typing import Any, Optional

import pandas as pd

from .dbapi import borrowed
from .warehouse import WarehouseError, quote_ident, relation_ref, validate_read_only_query

logger = logging.getLogger(__name__)

_IDENT = "`"
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
DEFAULT_SCRATCH_SCHEMA = "stych_temp"
PROBE_TIMEOUT_SECONDS = 60
WRITE_TIMEOUT_SECONDS = 600


class DatabricksLoadError(WarehouseError):
    """Raised when Databricks discovery or extraction fails."""


def connect_kwargs(config: Mapping[str, Any], *, socket_timeout: int = 60) -> dict[str, Any]:
    """Build databricks.sql.connect kwargs. Auth is PAT-only."""
    hostname = _hostname(str(config.get("server_hostname") or ""))
    http_path = str(config.get("http_path") or "").strip()
    token = str(config.get("access_token") or "").strip()
    catalog = str(config.get("catalog") or "").strip()
    if not hostname or not http_path or not token or not catalog:
        raise DatabricksLoadError(
            "Databricks authentication requires server_hostname, http_path, "
            "access_token, and catalog."
        )
    if not http_path.startswith("/"):
        http_path = "/" + http_path
    return {
        "server_hostname": hostname,
        "http_path": http_path,
        "access_token": token,
        "catalog": catalog,
        "_socket_timeout": int(socket_timeout),
    }


def _hostname(value: str) -> str:
    stripped = _SCHEME.sub("", value.strip())
    return stripped.split("/")[0].split("?")[0]


def _connect(config: Mapping[str, Any], *, socket_timeout: int = 60):
    try:
        from databricks import sql  # noqa: PLC0415
    except ImportError as exc:
        raise DatabricksLoadError(
            "Databricks support is not installed. Install the backend connector dependencies."
        ) from exc
    return sql.connect(**connect_kwargs(config, socket_timeout=socket_timeout))


def scratch_location(config: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(scratch_catalog, scratch_schema)`` with connect-time defaults."""
    catalog = str(config.get("scratch_catalog") or config.get("catalog") or "").strip()
    schema = str(config.get("scratch_schema") or DEFAULT_SCRATCH_SCHEMA).strip()
    return catalog, schema or DEFAULT_SCRATCH_SCHEMA


def normalize_scratch_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Copy *config* and fill scratch catalog/schema defaults."""
    out = dict(config)
    catalog, schema = scratch_location(out)
    out["scratch_catalog"] = catalog
    out["scratch_schema"] = schema
    return out


def _scratch_ref(config: Mapping[str, Any], table: str) -> str:
    catalog, schema = scratch_location(config)
    return relation_ref(database=catalog, schema=schema, table=table, quote=_IDENT)


def open_connection(config: Mapping[str, Any], *, socket_timeout: int = 60):
    """Open one Databricks SQL session. Callers must close it."""
    return _connect(config, socket_timeout=socket_timeout)


def _execute_on_connection(
    connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    fetch: bool,
):
    cursor = connection.cursor()
    try:
        if parameters:
            cursor.execute(statement, list(parameters))
        else:
            cursor.execute(statement)
        if not fetch:
            return None
        names = [str(item[0]) for item in (cursor.description or [])]
        return names, [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def execute_databricks_statement(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    timeout_seconds: int = WRITE_TIMEOUT_SECONDS,
    connection=None,
) -> None:
    """Run one DDL/DML statement. Does not fetch rows."""
    try:
        with borrowed(
            connection,
            lambda: _connect(config, socket_timeout=max(int(timeout_seconds), 8)),
        ) as conn:
            _execute_on_connection(conn, statement, parameters, fetch=False)
    except DatabricksLoadError:
        raise
    except Exception as exc:
        raise DatabricksLoadError(str(exc)) from exc


def run_databricks_query(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    timeout_seconds: int = 8,
    connection=None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    try:
        with borrowed(
            connection,
            lambda: _connect(config, socket_timeout=max(int(timeout_seconds), 8)),
        ) as conn:
            return _execute_on_connection(conn, statement, parameters, fetch=True)
    except DatabricksLoadError:
        raise
    except Exception as exc:
        raise DatabricksLoadError(str(exc)) from exc


def stream_databricks_query(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    batch_rows: int = 50_000,
    timeout_seconds: int = 1_800,
) -> "Iterator[tuple[list[str], list[tuple[Any, ...]]]]":
    """Yield ``(column_names, rows)`` chunks for one read statement.

    One ``execute`` fetched incrementally, so the result reflects a single query
    snapshot and never lands in memory whole. Used by the export path; callers
    must drain or close the generator to release the session.
    """
    try:
        with borrowed(
            None, lambda: _connect(config, socket_timeout=max(int(timeout_seconds), 8))
        ) as conn:
            cursor = conn.cursor()
            try:
                if parameters:
                    cursor.execute(statement, list(parameters))
                else:
                    cursor.execute(statement)
                names = [str(item[0]) for item in (cursor.description or [])]
                while True:
                    rows = cursor.fetchmany(int(batch_rows))
                    if not rows:
                        return
                    yield names, [tuple(row) for row in rows]
            finally:
                cursor.close()
    except DatabricksLoadError:
        raise
    except Exception as exc:
        raise DatabricksLoadError(str(exc)) from exc


def probe_scratch_permissions(config: Mapping[str, Any], *, connection=None) -> None:
    """Create the scratch schema and prove CREATE TABLE is allowed.

    Fail closed with a permission message rather than falling back to a sample overlay.
    Runs the three DDL statements on one session when *connection* is omitted.
    """
    catalog, schema = scratch_location(config)
    if not catalog:
        raise DatabricksLoadError(
            "Databricks catalog is required to create the scratch schema for full-data analysis."
        )
    qcat = quote_ident(catalog, _IDENT)
    qsch = quote_ident(schema, _IDENT)
    probe = f"_stych_probe_{uuid.uuid4().hex[:8]}"
    dest = _scratch_ref(config, probe)
    try:
        with borrowed(
            connection,
            lambda: _connect(config, socket_timeout=PROBE_TIMEOUT_SECONDS),
        ) as conn:
            execute_databricks_statement(
                config,
                f"CREATE SCHEMA IF NOT EXISTS {qcat}.{qsch}",
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
                connection=conn,
            )
            execute_databricks_statement(
                config,
                f"CREATE TABLE {dest} AS SELECT 1 AS ok",
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
                connection=conn,
            )
            execute_databricks_statement(
                config,
                f"DROP TABLE IF EXISTS {dest}",
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
                connection=conn,
            )
    except DatabricksLoadError as exc:
        raise DatabricksLoadError(
            f"Cannot create tables in {catalog}.{schema}. Grant CREATE SCHEMA and CREATE TABLE "
            f"on that scratch location (default is {catalog}.{DEFAULT_SCRATCH_SCHEMA}). "
            f"Original error: {exc}"
        ) from exc


def test_databricks_connection(config: Mapping[str, Any]) -> tuple[bool, str]:
    """Cheap liveness check. Scratch CREATE TABLE is probed when the connection is saved."""
    try:
        run_databricks_query(config, "SELECT 1", timeout_seconds=PROBE_TIMEOUT_SECONDS)
        return True, "Connection successful"
    except DatabricksLoadError as exc:
        return False, str(exc)


def validate_scratch_access(config: Mapping[str, Any]) -> None:
    """SELECT 1 plus scratch probe on a single session. Used when persisting a connection."""
    with borrowed(None, lambda: _connect(config, socket_timeout=PROBE_TIMEOUT_SECONDS)) as conn:
        run_databricks_query(
            config, "SELECT 1", timeout_seconds=PROBE_TIMEOUT_SECONDS, connection=conn
        )
        probe_scratch_permissions(config, connection=conn)


def list_databricks_tables(
    config: Mapping[str, Any], *, schema: str, limit: int = 1_000
) -> list[dict[str, str]]:
    catalog = str(config.get("catalog") or "").strip()
    if not catalog:
        raise DatabricksLoadError("Databricks catalog is required for table discovery.")
    statement = (
        f"SELECT table_schema, table_name FROM {quote_ident(catalog, _IDENT)}.information_schema.tables "
        "WHERE table_schema = ? AND table_type IN "
        "('TABLE', 'VIEW', 'EXTERNAL', 'MANAGED', 'BASE TABLE', 'MATERIALIZED_VIEW') "
        f"ORDER BY table_name LIMIT {int(limit)}"
    )
    _, rows = run_databricks_query(config, statement, (schema,))
    return [{"schema": str(row[0]), "table": str(row[1])} for row in rows]


def list_databricks_schemas(config: Mapping[str, Any], *, limit: int = 1_000) -> list[str]:
    """List schemas visible in the connection's configured catalog."""
    catalog = str(config.get("catalog") or "").strip()
    if not catalog:
        raise DatabricksLoadError("Databricks catalog is required for schema discovery.")
    statement = (
        f"SELECT schema_name FROM {quote_ident(catalog, _IDENT)}.information_schema.schemata "
        f"WHERE schema_name <> 'information_schema' ORDER BY schema_name LIMIT {int(limit)}"
    )
    _, rows = run_databricks_query(config, statement, timeout_seconds=PROBE_TIMEOUT_SECONDS)
    return [str(row[0]) for row in rows]


def load_from_databricks(
    config: Mapping[str, Any],
    *,
    schema: str,
    table: Optional[str] = None,
    custom_query: Optional[str] = None,
    preview_rows: int = 10_000,
    connection=None,
    probe_scratch: bool = False,
) -> tuple[pd.DataFrame, str]:
    if bool(table) == bool(custom_query):
        raise DatabricksLoadError("Provide either table or custom_query, not both.")
    catalog = str(config.get("catalog") or "").strip()
    if custom_query:
        try:
            query = validate_read_only_query(custom_query)
        except WarehouseError as exc:
            raise DatabricksLoadError(str(exc)) from exc
        statement = f"SELECT * FROM ({query}) AS _src LIMIT {int(preview_rows)}"
        label = "Custom query"
    else:
        if not catalog:
            raise DatabricksLoadError("Databricks catalog is required to load a table.")
        statement = (
            f"SELECT * FROM {relation_ref(database=catalog, schema=schema, table=str(table), quote=_IDENT)} "
            f"LIMIT {int(preview_rows)}"
        )
        label = f"{catalog}.{schema}.{table}"
    if connection is None and not probe_scratch:
        names, rows = run_databricks_query(config, statement, timeout_seconds=60)
        return pd.DataFrame.from_records(rows, columns=names), label
    with borrowed(connection, lambda: _connect(config, socket_timeout=60)) as conn:
        if probe_scratch:
            probe_scratch_permissions(config, connection=conn)
        names, rows = run_databricks_query(config, statement, timeout_seconds=60, connection=conn)
    return pd.DataFrame.from_records(rows, columns=names), label

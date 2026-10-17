"""Read-only Snowflake source edge using key-pair (JWT) authentication."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any, Optional

import pandas as pd
import pyarrow as pa

from .dbapi import borrowed
from .warehouse import WarehouseError, quote_ident, relation_ref, validate_read_only_query

logger = logging.getLogger(__name__)

_CONNECT_FIELDS = ("account", "user", "warehouse", "database", "role", "schema")


class SnowflakeLoadError(WarehouseError):
    """Raised when Snowflake discovery or extraction fails."""


def _private_key_der(pem: str, passphrase: Optional[str] = None) -> bytes:
    """Convert a PEM PKCS8/PKCS1 private key into unencrypted DER for the driver."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    secret = passphrase.encode() if passphrase else None
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=secret)
    except (TypeError, ValueError) as exc:
        raise SnowflakeLoadError(
            "Invalid Snowflake private key. Paste a PEM-encoded PKCS8 key; "
            "include the passphrase if the key is encrypted."
        ) from exc
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build snowflake.connector.connect kwargs. Never forwards a password."""
    pem = str(config.get("private_key") or "").strip()
    if not pem:
        raise SnowflakeLoadError("Snowflake key-pair authentication requires a private_key.")
    kwargs = {key: config[key] for key in _CONNECT_FIELDS if config.get(key) not in (None, "")}
    if "account" not in kwargs or "user" not in kwargs:
        raise SnowflakeLoadError("Snowflake account and user are required.")
    kwargs.update(
        {
            "private_key": _private_key_der(
                pem, str(config.get("private_key_passphrase") or "") or None
            ),
            "authenticator": "SNOWFLAKE_JWT",
            "application": "stych",
            "client_session_keep_alive": False,
            "login_timeout": 10,
            "network_timeout": 60,
            "paramstyle": "qmark",
        }
    )
    return kwargs


def _connect(config: Mapping[str, Any]):
    try:
        import snowflake.connector  # noqa: PLC0415
    except ImportError as exc:
        raise SnowflakeLoadError(
            "Snowflake support is not installed. Install the backend connector dependencies."
        ) from exc
    return snowflake.connector.connect(**connect_kwargs(config))


def open_connection(config: Mapping[str, Any]):
    """Open one Snowflake session. Callers must close it."""
    return _connect(config)


def run_snowflake_query(
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
                cursor.execute(statement, parameters, timeout=timeout_seconds)
                names = [str(item[0]) for item in (cursor.description or [])]
                return names, [tuple(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
    except SnowflakeLoadError:
        raise
    except Exception as exc:  # connector errors are optional-import types
        raise SnowflakeLoadError(str(exc)) from exc


def execute_snowflake_statement(
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
                cursor.execute(statement, parameters, timeout=timeout_seconds)
            finally:
                cursor.close()
    except SnowflakeLoadError:
        raise
    except Exception as exc:  # connector errors are optional-import types
        raise SnowflakeLoadError(str(exc)) from exc


def stream_snowflake_query(
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
                cursor.execute(statement, parameters, timeout=timeout_seconds)
                names = [str(item[0]) for item in (cursor.description or [])]
                while True:
                    rows = cursor.fetchmany(int(batch_rows))
                    if not rows:
                        return
                    yield names, [tuple(row) for row in rows]
            finally:
                cursor.close()
    except SnowflakeLoadError:
        raise
    except Exception as exc:  # connector errors are optional-import types
        raise SnowflakeLoadError(str(exc)) from exc


def test_snowflake_connection(config: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        run_snowflake_query(config, "SELECT 1")
        return True, "Connection successful"
    except SnowflakeLoadError as exc:
        return False, str(exc)


def list_snowflake_tables(
    config: Mapping[str, Any], *, database: str, schema: str, limit: int = 1_000
) -> list[dict[str, str]]:
    info_schema = f"{quote_ident(database)}.INFORMATION_SCHEMA.TABLES"
    statement = (
        f"SELECT TABLE_SCHEMA, TABLE_NAME FROM {info_schema} "
        "WHERE TABLE_SCHEMA = ? AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
        "ORDER BY TABLE_NAME LIMIT ?"
    )
    _, rows = run_snowflake_query(config, statement, (schema, int(limit)))
    return [{"schema": str(row[0]), "table": str(row[1])} for row in rows]


def list_snowflake_schemas(
    config: Mapping[str, Any], *, database: str, limit: int = 1_000
) -> list[str]:
    """List schemas visible in the connection's configured database."""
    info_schema = f"{quote_ident(database)}.INFORMATION_SCHEMA.SCHEMATA"
    statement = (
        f"SELECT SCHEMA_NAME FROM {info_schema} "
        "WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' ORDER BY SCHEMA_NAME LIMIT ?"
    )
    _, rows = run_snowflake_query(config, statement, (int(limit),))
    return [str(row[0]) for row in rows]


def _fetch_arrow_batches(cursor: Any) -> Iterator[pa.Table]:
    for batch in cursor.fetch_arrow_batches():
        if batch is not None:
            yield batch


def load_from_snowflake(
    config: Mapping[str, Any],
    *,
    database: str,
    schema: str,
    table: Optional[str] = None,
    custom_query: Optional[str] = None,
    preview_rows: int = 10_000,
) -> tuple[pd.DataFrame, str]:
    if bool(table) == bool(custom_query):
        raise SnowflakeLoadError("Provide either table or custom_query, not both.")
    if custom_query:
        try:
            query = validate_read_only_query(custom_query)
        except WarehouseError as exc:
            raise SnowflakeLoadError(str(exc)) from exc
        statement = f"SELECT * FROM ({query}) AS _src LIMIT {int(preview_rows)}"
        label = "Custom query"
    else:
        statement = (
            f"SELECT * FROM {relation_ref(database=database, schema=schema, table=str(table))} "
            f"LIMIT {int(preview_rows)}"
        )
        label = f"{database}.{schema}.{table}"

    connection = None
    cursor = None
    try:
        connection = _connect(config)
        cursor = connection.cursor()
        cursor.execute(statement, timeout=60)
        batches = list(_fetch_arrow_batches(cursor))
        if batches:
            arrow_table = pa.concat_tables(batches, promote_options="default")
        else:
            names = [str(item[0]) for item in (cursor.description or [])]
            arrow_table = pa.table({name: [] for name in names})
        return arrow_table.to_pandas(), label
    except SnowflakeLoadError:
        raise
    except Exception as exc:
        logger.error("Snowflake load failed: %s", exc)
        raise SnowflakeLoadError(str(exc)) from exc
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()

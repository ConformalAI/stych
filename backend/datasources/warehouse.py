"""Shared, credential-safe helpers for read-only cloud warehouse connectors."""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from .sql_readonly import ReadOnlySQLError, parse_read_only_statements


class WarehouseError(Exception):
    """Base error raised by cloud warehouse source modules."""


def quote_ident(name: str, quote: str = '"') -> str:
    """Quote an identifier while preserving its exact discovered case."""
    q = quote or '"'
    text = str(name)
    if q == "`":
        return q + text.replace("\\", "\\\\").replace("`", "\\`") + q
    return q + text.replace(q, q + q) + q


def relation_ref(*, database: Optional[str], schema: str, table: str, quote: str = '"') -> str:
    parts = [database, schema, table] if database else [schema, table]
    return ".".join(quote_ident(part, quote) for part in parts if part)


def validate_read_only_query(query: str) -> str:
    """Accept exactly one SELECT/WITH statement and reject embedded writes."""
    stripped = (query or "").strip().rstrip(";").strip()
    if not stripped:
        raise WarehouseError("custom_query is required")
    try:
        statements = parse_read_only_statements(
            stripped, allowed_starts=frozenset({"SELECT", "WITH"})
        )
    except ReadOnlySQLError as exc:
        raise WarehouseError(f"{exc} Only read-only SELECT statements are permitted.") from exc
    if len(statements) != 1:
        raise WarehouseError("custom_query must be a single statement")
    return statements[0]


def encode_connection_config(config: Mapping[str, Any]) -> str:
    """Serialize connector credentials for encrypted-at-rest DataContext metadata."""
    return json.dumps(dict(config), separators=(",", ":"), sort_keys=True)


def decode_connection_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value or value == "[REDACTED]":
        raise WarehouseError("Warehouse credentials are unavailable; reconnect the source.")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WarehouseError("Warehouse credentials are invalid; reconnect the source.") from exc
    if not isinstance(parsed, dict):
        raise WarehouseError("Warehouse credentials are invalid; reconnect the source.")
    return parsed

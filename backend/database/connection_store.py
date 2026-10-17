"""Per-user saved datasource connections (encrypted credentials)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from database.models import UserConnection, UserConnectionTable
from helpers.secrets_crypto import get_fernet as _fernet

logger = logging.getLogger(__name__)

CONNECTOR_IDS = frozenset(
    {
        "postgres",
        "sqlserver",
        "mongodb",
        "snowflake",
        "redshift",
        "bigquery",
        "databricks",
    }
)

_HINT_KEYS = (
    "host",
    "port",
    "account",
    "warehouse",
    "database",
    "catalog",
    "project_id",
    "user",
    "server_hostname",
    "http_path",
    "scratch_catalog",
    "scratch_schema",
    "cluster_identifier",
    "region",
    "sslmode",
    "auth_mode",
    "use_default",
    "role",
    "access_key_id",
)

_SECRET_KEYS = frozenset(
    {
        "password",
        "private_key",
        "private_key_passphrase",
        "access_token",
        "secret_access_key",
        "session_token",
        "service_account_json",
        "connection_url",
        "refresh_token",
        "client_id",
        "expiry_date",
    }
)


def encrypt_credentials(creds: dict[str, Any]) -> str:
    return (
        _fernet()
        .encrypt(json.dumps(creds, separators=(",", ":"), sort_keys=True).encode())
        .decode()
    )


def decrypt_credentials(blob: str) -> dict[str, Any]:
    decrypted = _fernet().decrypt(blob.encode()).decode()
    parsed = json.loads(decrypted)
    if not isinstance(parsed, dict):
        raise ValueError("credentials blob is not an object")
    return parsed


def public_hint(
    credentials: dict[str, Any], extra: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    hint: dict[str, Any] = {}
    url = credentials.get("connection_url")
    if isinstance(url, str) and url.strip():
        hint.update(_url_hint(url))
    for key in _HINT_KEYS:
        value = credentials.get(key)
        # A connection URL is the authority for endpoint fields. Pydantic
        # component defaults (for example SQL Server port 1433) must not
        # overwrite the actual URL endpoint shown on the saved connector card.
        if key not in hint and value not in (None, ""):
            hint[key] = value
    for key, value in (extra or {}).items():
        if value not in (None, ""):
            hint[key] = value
    return {k: v for k, v in hint.items() if k not in _SECRET_KEYS}


def merge_connection_update(stored: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial edit. Blank values keep the stored secret or field."""
    merged = dict(stored)
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        merged[key] = value
    if incoming.get("connection_url"):
        merged.pop("use_default", None)
    elif incoming.get("use_default") is True:
        merged.pop("connection_url", None)
        merged["use_default"] = True
    return merged


def _url_hint(url: str) -> dict[str, Any]:
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    if parts.hostname:
        out["host"] = parts.hostname
    if parts.port:
        out["port"] = parts.port
    if parts.username:
        out["user"] = parts.username
    database = (parts.path or "").lstrip("/")
    if database:
        out["database"] = database.split("/")[0]
    return out


def _parse_config(row: UserConnection) -> dict[str, Any]:
    raw = row.config_json
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def table_to_dict(row: UserConnectionTable) -> dict[str, Any]:
    return {
        "id": row.id,
        "schema_name": row.schema_name,
        "table_name": row.table_name,
        "custom_query": row.custom_query,
        "display_name": row.display_name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def parse_tags(row: UserConnection) -> list[str]:
    """User-defined tags, normalized. Malformed JSON degrades to no tags."""
    raw = row.tags_json
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [t.strip() for t in parsed if isinstance(t, str) and t.strip()]


def normalize_tags(tags: list[str]) -> list[str]:
    """Trim, lowercase, drop blanks/dupes, preserve order, cap length and count."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = tag.strip().lower()[:32]
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
        if len(out) >= 20:
            break
    return out


def connection_to_dict(row: UserConnection, *, include_tables: bool = True) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "connector_id": row.connector_id,
        "name": row.name,
        "description": row.description or None,
        "tags": parse_tags(row),
        "hint": _parse_config(row),
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_test_status": row.last_test_status or None,
        "last_test_error": row.last_test_error or None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_tables:
        payload["tables"] = [table_to_dict(t) for t in (row.tables or [])]
    return payload


def get_connection(db: Session, app_user_id: str, connection_id: str) -> Optional[UserConnection]:
    return (
        db.query(UserConnection)
        .filter(UserConnection.id == connection_id, UserConnection.app_user_id == app_user_id)
        .first()
    )


def list_connections(
    db: Session, app_user_id: str, connector_id: Optional[str] = None
) -> list[UserConnection]:
    query = db.query(UserConnection).filter(UserConnection.app_user_id == app_user_id)
    if connector_id:
        query = query.filter(UserConnection.connector_id == connector_id)
    return query.order_by(UserConnection.created_at.desc()).all()


def create_connection(
    db: Session,
    *,
    app_user_id: str,
    connector_id: str,
    name: str,
    credentials: dict[str, Any],
    hint: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> UserConnection:
    now = datetime.utcnow()
    row = UserConnection(
        id=str(uuid.uuid4()),
        app_user_id=app_user_id,
        connector_id=connector_id,
        name=name.strip() or connector_id,
        credentials_enc=encrypt_credentials(credentials),
        config_json=json.dumps(hint or public_hint(credentials), separators=(",", ":")),
        description=(description or "").strip() or None,
        tags_json=json.dumps(normalize_tags(tags or []), separators=(",", ":")),
        last_tested_at=now,
        # create_connection is only reached after a successful test_and_normalize.
        last_test_status="ok",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_connection(
    db: Session,
    row: UserConnection,
    *,
    name: Optional[str] = None,
    credentials: Optional[dict[str, Any]] = None,
    hint: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> UserConnection:
    now = datetime.utcnow()
    if name is not None and name.strip():
        row.name = name.strip()
    if description is not None:
        row.description = description.strip() or None
    if tags is not None:
        row.tags_json = json.dumps(normalize_tags(tags), separators=(",", ":"))
    if credentials is not None:
        row.credentials_enc = encrypt_credentials(credentials)
        row.config_json = json.dumps(hint or public_hint(credentials), separators=(",", ":"))
        row.last_tested_at = now
        # A credential update only lands after a successful re-test.
        row.last_test_status = "ok"
        row.last_test_error = None
    elif hint is not None:
        row.config_json = json.dumps(hint, separators=(",", ":"))
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


def record_test_result(
    db: Session, row: UserConnection, *, ok: bool, error: Optional[str] = None
) -> UserConnection:
    """Persist the outcome of a connection test so the UI can show health."""
    row.last_tested_at = datetime.utcnow()
    row.last_test_status = "ok" if ok else "error"
    row.last_test_error = None if ok else (error or "Connection failed")[:2000]
    db.commit()
    db.refresh(row)
    return row


def touch_last_used(db: Session, row: UserConnection) -> None:
    """Mark the connection as used — called when a table is attached to a session."""
    row.last_used_at = datetime.utcnow()
    db.commit()


def delete_connection(db: Session, row: UserConnection) -> None:
    db.delete(row)
    db.commit()


def load_credentials(row: UserConnection) -> dict[str, Any]:
    try:
        return decrypt_credentials(row.credentials_enc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to decrypt connection %s: %s", row.id, exc)
        raise ValueError(
            "Saved connection credentials could not be decrypted; update the connection."
        ) from exc


def pin_table(
    db: Session,
    row: UserConnection,
    *,
    schema_name: Optional[str],
    table_name: Optional[str],
    custom_query: Optional[str],
    display_name: str,
) -> UserConnectionTable:
    schema_name = (schema_name or "").strip() or None
    table_name = (table_name or "").strip() or None
    custom_query = (custom_query or "").strip() or None
    if not custom_query and not table_name:
        raise ValueError("table_name or custom_query is required")
    existing = (
        db.query(UserConnectionTable)
        .filter(
            UserConnectionTable.connection_id == row.id,
            UserConnectionTable.schema_name == schema_name,
            UserConnectionTable.table_name == table_name,
        )
        .first()
    )
    if existing and not custom_query:
        return existing
    pin = UserConnectionTable(
        id=str(uuid.uuid4()),
        connection_id=row.id,
        schema_name=schema_name,
        table_name=table_name,
        custom_query=custom_query,
        display_name=display_name.strip() or table_name or "query",
        created_at=datetime.utcnow(),
    )
    db.add(pin)
    db.commit()
    db.refresh(pin)
    return pin


def get_pinned_table(
    db: Session, row: UserConnection, table_id: str
) -> Optional[UserConnectionTable]:
    return (
        db.query(UserConnectionTable)
        .filter(UserConnectionTable.id == table_id, UserConnectionTable.connection_id == row.id)
        .first()
    )


def unpin_table(db: Session, pin: UserConnectionTable) -> None:
    db.delete(pin)
    db.commit()

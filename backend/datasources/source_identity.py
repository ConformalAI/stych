"""Stable identity for the *connection* a DataContext reads through.

Two contexts whose keys are equal and non-``None`` live inside one engine, so a
join between them can be pushed into the source instead of materialized here.
The key is deliberately about the connection, not the relation: Postgres cannot
query across databases, while Databricks joins across catalogs in one workspace,
so each connector decides what "same engine" means for it.

Identity includes the *principal* (Postgres role, Databricks token). A pushed
join executes through exactly one connection, so two relations reached with
different credentials are not safely joinable — the executing role may not see
both. Absent or redacted credentials yield ``None``, which never matches.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)


def _fingerprint(*parts: Any) -> str:
    """Short, non-reversible digest of secret-bearing identity parts."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _redacted(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or text == "[REDACTED]"


def _postgres_key(meta: Mapping[str, Any]) -> Optional[str]:
    """Host/port/database/role. Postgres has no cross-database queries, so the
    database name is part of identity; the role is, because it bounds visibility."""
    url = meta.get("connection_url")
    if _redacted(url) or "://" not in str(url):
        return None
    from sqlalchemy.engine import make_url  # noqa: PLC0415

    try:
        parsed = make_url(str(url))
    except Exception:  # noqa: BLE001 — an unparseable URL is not an identity
        return None
    if not parsed.database:
        return None
    return "postgres:" + _fingerprint(
        parsed.host, parsed.port or 5432, parsed.database, parsed.username
    )


def _databricks_key(meta: Mapping[str, Any]) -> Optional[str]:
    """Workspace host + SQL warehouse + token. Unity Catalog joins across
    catalogs through one warehouse, so the catalog is *not* part of identity."""
    config = _decode_config(meta)
    if config is None:
        return None
    host = str(config.get("server_hostname") or "").strip()
    http_path = str(config.get("http_path") or "").strip()
    token = str(config.get("access_token") or "").strip()
    if not host or not http_path or _redacted(token):
        return None
    return "databricks:" + _fingerprint(host.lower(), http_path, token)


def _bigquery_key(meta: Mapping[str, Any]) -> Optional[str]:
    """Project plus the credential principal used by the BigQuery client.

    The project is the billing/default project for the client, not a relation
    namespace: a BigQuery query may address other projects when the principal
    has permission.  We nevertheless include it because Stych constructs its
    runner from this exact config and must not silently merge differently
    configured clients.  Access tokens are intentionally not used as the
    identity; OAuth refresh tokens and service-account emails are stable
    principals while access tokens rotate.
    """
    config = _decode_config(meta)
    if config is None:
        return None
    project = str(config.get("project_id") or meta.get("database") or "").strip()
    if not project:
        return None
    auth_mode = str(config.get("auth_mode") or "").lower()
    if auth_mode == "oauth" or config.get("refresh_token"):
        refresh = str(config.get("refresh_token") or "").strip()
        client_id = str(config.get("client_id") or "").strip()
        if _redacted(refresh) or not client_id:
            return None
        principal = ("oauth", client_id, refresh)
    else:
        raw = str(config.get("service_account_json") or "").strip()
        if _redacted(raw):
            return None
        try:
            info = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(info, dict):
            return None
        principal_email = str(info.get("client_email") or "").strip()
        if info.get("type") != "service_account" or not principal_email:
            return None
        if _redacted(info.get("private_key")):
            return None
        principal = ("service_account", principal_email)
    return "bigquery:" + _fingerprint(project.lower(), *principal)


def _snowflake_key(meta: Mapping[str, Any]) -> Optional[str]:
    """Account/user/warehouse/database/role for one Snowflake execution context."""
    config = _decode_config(meta)
    if config is None:
        return None
    account = str(config.get("account") or "").strip()
    user = str(config.get("user") or "").strip()
    warehouse = str(config.get("warehouse") or "").strip()
    database = str(config.get("database") or meta.get("database") or "").strip()
    role = str(config.get("role") or "").strip()
    private_key = str(config.get("private_key") or "").strip()
    if not account or not user or not warehouse or not database or _redacted(private_key):
        return None
    return "snowflake:" + _fingerprint(
        account.lower(), user.lower(), warehouse.lower(), database.lower(), role.lower()
    )


def _redshift_key(meta: Mapping[str, Any]) -> Optional[str]:
    """Cluster endpoint/database/user plus the IAM access-key principal."""
    config = _decode_config(meta)
    if config is None:
        return None
    host = str(config.get("host") or "").strip()
    port = str(config.get("port") or 5439).strip()
    database = str(config.get("database") or meta.get("database") or "").strip()
    user = str(config.get("user") or config.get("db_user") or "").strip()
    access_key = str(config.get("access_key_id") or "").strip()
    secret = str(config.get("secret_access_key") or "").strip()
    if not host or not database or not user or not access_key or _redacted(secret):
        return None
    return "redshift:" + _fingerprint(
        host.lower(),
        port,
        database,
        user,
        access_key,
        str(config.get("cluster_identifier") or "").strip(),
    )


def _sqlserver_key(meta: Mapping[str, Any]) -> Optional[str]:
    """SQLAlchemy URL identity for one SQL Server database connection.

    SQL Server's current runner addresses two-part ``schema.table`` names in
    the connection's default database, so the database remains part of the
    identity even though T-SQL can sometimes reach other databases with
    three-part names.
    """
    url = meta.get("connection_url")
    if _redacted(url) or "://" not in str(url):
        return None
    from sqlalchemy.engine import make_url  # noqa: PLC0415

    try:
        parsed = make_url(str(url))
    except Exception:  # noqa: BLE001 — an unparseable URL is not an identity
        return None
    if parsed.drivername.split("+")[0].lower() not in {"mssql", "sqlserver"}:
        return None
    if not parsed.host or not parsed.database or _redacted(parsed.password):
        return None
    query = tuple(sorted((str(k), str(v)) for k, v in (parsed.query or {}).items()))
    return "sqlserver:" + _fingerprint(
        parsed.host.lower(),
        parsed.port or 1433,
        parsed.database,
        parsed.username or "",
        query,
    )


def _mongodb_key(meta: Mapping[str, Any]) -> Optional[str]:
    """Connection plus database, matching MongoDB's ``$lookup`` boundary.

    MongoDB cannot join collections across databases with ``$lookup``.  The
    exact URI is fingerprinted rather than normalized: treating two equivalent
    spellings as different only selects the safe full-stream fallback, while
    treating different replica-set/auth options as equal could execute with the
    wrong visibility.
    """
    url = str(meta.get("connection_url") or "").strip()
    database = str(meta.get("database") or "").strip()
    if _redacted(url) or "[REDACTED]" in url or "://" not in url or not database:
        return None
    return "mongodb:" + _fingerprint(url, database)


def _decode_config(meta: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    raw = meta.get("connection_config")
    if _redacted(raw):
        return None
    from .warehouse import WarehouseError, decode_connection_config  # noqa: PLC0415

    try:
        return decode_connection_config(raw)
    except WarehouseError:
        return None


# Connectors able to execute a join inside the source. Absent connectors return
# None (never joinable in-source), so adding one is opt-in rather than assumed.
_KEY_BUILDERS: dict[str, Callable[[Mapping[str, Any]], Optional[str]]] = {
    "postgres": _postgres_key,
    "databricks": _databricks_key,
    "bigquery": _bigquery_key,
    "snowflake": _snowflake_key,
    "redshift": _redshift_key,
    "sqlserver": _sqlserver_key,
    "mongodb": _mongodb_key,
}


def source_key(meta: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Identity of the engine behind *meta*, or ``None`` when not established."""
    if not meta:
        return None
    builder = _KEY_BUILDERS.get(str(meta.get("source") or ""))
    if builder is None:
        return None
    try:
        return builder(meta)
    except Exception as exc:  # noqa: BLE001 — identity must fail closed
        logger.debug("source_key failed for %s: %s", meta.get("source"), exc)
        return None


def same_source(left: Optional[Mapping[str, Any]], right: Optional[Mapping[str, Any]]) -> bool:
    """True when both contexts are reachable through one connection."""
    a, b = source_key(left), source_key(right)
    return a is not None and a == b

"""Read-only BigQuery source edge: service-account JSON or Google user OAuth."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Optional

import pandas as pd

from .dbapi import borrowed
from .warehouse import WarehouseError, quote_ident, relation_ref, validate_read_only_query

logger = logging.getLogger(__name__)

_IDENT = "`"
# Discovery statements (SELECT 1, INFORMATION_SCHEMA) are cheap; analytical
# reads against real tables are not. Callers pick the budget that fits.
DISCOVERY_TIMEOUT_SECONDS = 8
QUERY_TIMEOUT_SECONDS = 120
# Per-query scan cap. A connection may lower it via ``max_bytes_billed``.
DEFAULT_MAX_BYTES_BILLED = 10 * 1024**3
OAUTH_SERVER_ID = "google_bigquery"
_OAUTH_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"


class BigQueryLoadError(WarehouseError):
    """Raised when BigQuery discovery or extraction fails."""


def is_oauth(config: Mapping[str, Any]) -> bool:
    if str(config.get("auth_mode") or "").lower() == "oauth":
        return True
    if config.get("refresh_token") or config.get("access_token"):
        return not bool(str(config.get("service_account_json") or "").strip())
    return False


def resolved_project(config: Mapping[str, Any]) -> str:
    if is_oauth(config):
        project = str(config.get("project_id") or "").strip()
        if not project:
            raise BigQueryLoadError("project_id is required after connecting with Google.")
        return project
    info = _service_account_info(config)
    project = str(config.get("project_id") or info.get("project_id") or "").strip()
    if not project:
        raise BigQueryLoadError(
            "project_id is required, or must be present on the service account JSON."
        )
    return project


def _service_account_info(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(config.get("service_account_json") or "").strip()
    if not raw:
        raise BigQueryLoadError("BigQuery authentication requires service_account_json.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BigQueryLoadError("service_account_json must be valid JSON.") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise BigQueryLoadError(
            "service_account_json must be a Google service account key (type == 'service_account')."
        )
    if not info.get("private_key") or not info.get("client_email"):
        raise BigQueryLoadError("service_account_json is missing private_key or client_email.")
    return info


def connect_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    """Parse stored credentials. Never forwards a user password."""
    if is_oauth(config):
        if not (config.get("access_token") or config.get("refresh_token")):
            raise BigQueryLoadError("Connect your Google account before querying BigQuery.")
        return {
            "auth_mode": "oauth",
            "access_token": config.get("access_token"),
            "refresh_token": config.get("refresh_token"),
            "client_id": config.get("client_id"),
            "project_id": str(config.get("project_id") or "").strip() or None,
        }
    info = _service_account_info(config)
    return {"service_account_info": info, "project_id": resolved_project(config)}


def _client(config: Mapping[str, Any]):
    try:
        from google.cloud import bigquery  # noqa: PLC0415
    except ImportError as exc:
        raise BigQueryLoadError(
            "BigQuery support is not installed. Install the backend connector dependencies."
        ) from exc
    kwargs = connect_kwargs(config)
    if kwargs.get("auth_mode") == "oauth":
        credentials = _user_credentials(kwargs)
        return bigquery.Client(project=kwargs.get("project_id"), credentials=credentials)
    from google.oauth2 import service_account  # noqa: PLC0415

    credentials = service_account.Credentials.from_service_account_info(
        kwargs["service_account_info"],
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(project=kwargs["project_id"], credentials=credentials)


def _user_credentials(kwargs: Mapping[str, Any]):
    try:
        from google.auth.transport.requests import Request  # noqa: PLC0415
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
    except ImportError as exc:
        raise BigQueryLoadError(
            "BigQuery support is not installed. Install the backend connector dependencies."
        ) from exc
    credentials = Credentials(
        token=kwargs.get("access_token") or None,
        refresh_token=kwargs.get("refresh_token") or None,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=kwargs.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None,
        client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or None,
        scopes=[_OAUTH_SCOPE],
    )
    if not credentials.valid and credentials.refresh_token:
        credentials.refresh(Request())
    return credentials


def _scalar(value: Any):
    from google.cloud.bigquery import ScalarQueryParameter  # noqa: PLC0415

    if isinstance(value, bool):
        return ScalarQueryParameter(None, "BOOL", value)
    if isinstance(value, int):
        return ScalarQueryParameter(None, "INT64", value)
    if isinstance(value, float):
        return ScalarQueryParameter(None, "FLOAT64", value)
    return ScalarQueryParameter(None, "STRING", str(value))


def open_client(config: Mapping[str, Any]):
    """Open one BigQuery client. Callers must close it."""
    return _client(config)


def max_bytes_billed(config: Mapping[str, Any]) -> int:
    """Scan cap for one query: the connection's ``max_bytes_billed`` or the default."""
    try:
        value = int(config.get("max_bytes_billed") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else DEFAULT_MAX_BYTES_BILLED


def run_bigquery_query(
    config: Mapping[str, Any],
    statement: str,
    parameters: tuple[Any, ...] = (),
    *,
    timeout_seconds: int = QUERY_TIMEOUT_SECONDS,
    client=None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run one read. Discovery callers pass ``DISCOVERY_TIMEOUT_SECONDS``."""
    from google.cloud.bigquery import QueryJobConfig  # noqa: PLC0415

    try:
        with borrowed(client, lambda: _client(config)) as handle:
            job = handle.query(
                statement,
                job_config=QueryJobConfig(
                    query_parameters=[_scalar(value) for value in parameters],
                    use_legacy_sql=False,
                    maximum_bytes_billed=max_bytes_billed(config),
                    job_timeout_ms=int(timeout_seconds * 1000),
                ),
            )
            result = job.result(timeout=timeout_seconds)
            names = [field.name for field in (result.schema or [])]
            return names, [tuple(row) for row in result]
    except BigQueryLoadError:
        raise
    except Exception as exc:
        raise BigQueryLoadError(str(exc)) from exc


def test_bigquery_connection(config: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        run_bigquery_query(config, "SELECT 1", timeout_seconds=DISCOVERY_TIMEOUT_SECONDS)
        return True, "Connection successful"
    except BigQueryLoadError as exc:
        return False, str(exc)


def list_bigquery_projects(config: Mapping[str, Any]) -> list[str]:
    client = None
    try:
        client = _client(config)
        return [str(project.project_id) for project in client.list_projects()]
    except BigQueryLoadError:
        raise
    except Exception as exc:
        raise BigQueryLoadError(str(exc)) from exc
    finally:
        if client is not None:
            client.close()


def list_bigquery_datasets(config: Mapping[str, Any], *, limit: int = 1_000) -> list[str]:
    project = resolved_project(config)
    statement = (
        f"SELECT schema_name FROM {quote_ident(project, _IDENT)}.INFORMATION_SCHEMA.SCHEMATA "
        f"ORDER BY schema_name LIMIT {int(limit)}"
    )
    _, rows = run_bigquery_query(config, statement, timeout_seconds=DISCOVERY_TIMEOUT_SECONDS)
    return [str(row[0]) for row in rows]


def list_bigquery_tables(
    config: Mapping[str, Any], *, schema: str, limit: int = 1_000
) -> list[dict[str, str]]:
    dataset = str(schema or "").strip()
    if not dataset:
        raise BigQueryLoadError("BigQuery dataset is required for table discovery.")
    project = resolved_project(config)
    statement = (
        f"SELECT table_schema, table_name FROM {quote_ident(project, _IDENT)}."
        f"{quote_ident(dataset, _IDENT)}.INFORMATION_SCHEMA.TABLES "
        "WHERE table_type IN ('BASE TABLE', 'VIEW') "
        f"ORDER BY table_name LIMIT {int(limit)}"
    )
    _, rows = run_bigquery_query(config, statement, timeout_seconds=DISCOVERY_TIMEOUT_SECONDS)
    return [{"schema": str(row[0]), "table": str(row[1])} for row in rows]


def load_from_bigquery(
    config: Mapping[str, Any],
    *,
    schema: str,
    table: Optional[str] = None,
    custom_query: Optional[str] = None,
    preview_rows: int = 10_000,
) -> tuple[pd.DataFrame, str]:
    if bool(table) == bool(custom_query):
        raise BigQueryLoadError("Provide either table or custom_query, not both.")
    if not str(schema or "").strip():
        raise BigQueryLoadError("BigQuery dataset is required.")
    project = resolved_project(config)
    if custom_query:
        try:
            query = validate_read_only_query(custom_query)
        except WarehouseError as exc:
            raise BigQueryLoadError(str(exc)) from exc
        statement = f"SELECT * FROM ({query}) AS _src LIMIT {int(preview_rows)}"
        label = "Custom query"
    else:
        statement = (
            f"SELECT * FROM {relation_ref(database=project, schema=schema, table=str(table), quote=_IDENT)} "
            f"LIMIT {int(preview_rows)}"
        )
        label = f"{project}.{schema}.{table}"
    names, rows = run_bigquery_query(config, statement, timeout_seconds=QUERY_TIMEOUT_SECONDS)
    return pd.DataFrame.from_records(rows, columns=names), label

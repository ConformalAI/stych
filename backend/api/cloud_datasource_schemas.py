"""API models for read-only cloud warehouse connectors."""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import DatasourceLoadResponse


class WarehouseStatusResponse(BaseModel):
    enabled: bool


class WarehouseTestResponse(BaseModel):
    success: bool
    message: str


class WarehouseTablesResponse(BaseModel):
    schema: str
    tables: list[dict[str, str]]


class BigQueryOAuthStatusResponse(BaseModel):
    connected: bool


class BigQueryProjectsResponse(BaseModel):
    projects: list[str]


class BigQueryDatasetsResponse(BaseModel):
    datasets: list[str]


class SnowflakeConnectionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1)
    user: str = Field(min_length=1)
    private_key: str = Field(min_length=1)
    warehouse: str = Field(min_length=1)
    database: str = Field(min_length=1)
    role: Optional[str] = None
    private_key_passphrase: Optional[str] = None

    @field_validator("private_key")
    @classmethod
    def _pem_private_key(cls, value: str) -> str:
        stripped = value.strip()
        if "PRIVATE KEY" not in stripped.upper():
            raise ValueError("private_key must be a PEM-encoded PKCS8 or PKCS1 private key")
        return stripped

    @field_validator("role", "private_key_passphrase", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class SnowflakeTestRequest(BaseModel):
    connection: SnowflakeConnectionParams


class SnowflakeTablesRequest(BaseModel):
    connection: SnowflakeConnectionParams
    schema: str = "PUBLIC"


class SnowflakeLoadRequest(BaseModel):
    session_id: Optional[str] = None
    connection: SnowflakeConnectionParams
    schema: str = "PUBLIC"
    table: Optional[str] = None
    custom_query: Optional[str] = None
    connection_id: Optional[str] = None


class RedshiftConnectionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(default=5439, ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    access_key_id: str = Field(min_length=1)
    secret_access_key: str = Field(min_length=1)
    session_token: Optional[str] = None
    region: Optional[str] = None
    cluster_identifier: Optional[str] = None

    @field_validator("session_token", "region", "cluster_identifier", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class RedshiftTestRequest(BaseModel):
    connection: RedshiftConnectionParams


class RedshiftTablesRequest(BaseModel):
    connection: RedshiftConnectionParams
    schema: str = "public"


class RedshiftLoadRequest(BaseModel):
    session_id: Optional[str] = None
    connection: RedshiftConnectionParams
    schema: str = "public"
    table: Optional[str] = None
    custom_query: Optional[str] = None
    connection_id: Optional[str] = None


class BigQueryConnectionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_mode: Optional[str] = None
    service_account_json: Optional[str] = None
    project_id: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expiry_date: Optional[int] = None
    client_id: Optional[str] = None

    @field_validator("service_account_json", "auth_mode", "project_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("service_account_json")
    @classmethod
    def _service_account(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError("service_account_json must be valid JSON") from exc
        if not isinstance(parsed, dict) or parsed.get("type") != "service_account":
            raise ValueError(
                "service_account_json must be a Google service account key (type == 'service_account')"
            )
        if not parsed.get("private_key") or not parsed.get("client_email"):
            raise ValueError("service_account_json is missing private_key or client_email")
        return stripped

    @field_validator("auth_mode")
    @classmethod
    def _auth_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        mode = value.strip().lower()
        if mode != "oauth":
            raise ValueError("auth_mode must be oauth when set")
        return mode

    @model_validator(mode="after")
    def _require_auth(self) -> BigQueryConnectionParams:
        if self.auth_mode == "oauth":
            return self
        if not self.service_account_json:
            raise ValueError("service_account_json is required unless auth_mode is oauth")
        return self


class BigQueryTestRequest(BaseModel):
    connection: BigQueryConnectionParams


class BigQueryTablesRequest(BaseModel):
    connection: BigQueryConnectionParams
    schema: str = Field(min_length=1)


class BigQueryLoadRequest(BaseModel):
    session_id: Optional[str] = None
    connection: BigQueryConnectionParams
    schema: str = Field(min_length=1)
    table: Optional[str] = None
    custom_query: Optional[str] = None
    connection_id: Optional[str] = None


class DatabricksConnectionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_hostname: str = Field(min_length=1)
    http_path: str = Field(min_length=1)
    access_token: str = Field(min_length=1)
    catalog: str = Field(min_length=1)
    scratch_catalog: Optional[str] = None
    scratch_schema: Optional[str] = None

    @field_validator("server_hostname")
    @classmethod
    def _hostname(cls, value: str) -> str:
        stripped = value.strip()
        if stripped.lower().startswith("http://") or stripped.lower().startswith("https://"):
            stripped = stripped.split("://", 1)[1]
        return stripped.split("/")[0].split("?")[0]

    @field_validator("http_path")
    @classmethod
    def _http_path(cls, value: str) -> str:
        stripped = value.strip()
        return stripped if stripped.startswith("/") else f"/{stripped}"


class DatabricksTestRequest(BaseModel):
    connection: DatabricksConnectionParams


class DatabricksTablesRequest(BaseModel):
    connection: DatabricksConnectionParams
    schema: str = "default"


class DatabricksLoadRequest(BaseModel):
    session_id: Optional[str] = None
    connection: DatabricksConnectionParams
    schema: str = "default"
    table: Optional[str] = None
    custom_query: Optional[str] = None
    connection_id: Optional[str] = None


__all__ = [
    "BigQueryDatasetsResponse",
    "BigQueryLoadRequest",
    "BigQueryOAuthStatusResponse",
    "BigQueryProjectsResponse",
    "BigQueryTablesRequest",
    "BigQueryTestRequest",
    "DatabricksLoadRequest",
    "DatabricksTablesRequest",
    "DatabricksTestRequest",
    "DatasourceLoadResponse",
    "RedshiftLoadRequest",
    "RedshiftTablesRequest",
    "RedshiftTestRequest",
    "SnowflakeLoadRequest",
    "SnowflakeTablesRequest",
    "SnowflakeTestRequest",
    "WarehouseStatusResponse",
    "WarehouseTablesResponse",
    "WarehouseTestResponse",
]

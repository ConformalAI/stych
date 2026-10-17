"""API models for the user-level connector library."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .schemas import DatasourceLoadResponse


class ConnectionHint(BaseModel):
    model_config = {"extra": "allow"}


class SavedTable(BaseModel):
    id: str
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    custom_query: Optional[str] = None
    display_name: str
    created_at: Optional[str] = None


class SavedConnection(BaseModel):
    id: str
    connector_id: str
    name: str
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    hint: dict[str, Any] = Field(default_factory=dict)
    last_tested_at: Optional[str] = None
    # "ok" | "error" | None (never tested)
    last_test_status: Optional[str] = None
    last_test_error: Optional[str] = None
    last_used_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    tables: list[SavedTable] = Field(default_factory=list)


class SavedConnectionList(BaseModel):
    connections: list[SavedConnection]


class CreateConnectionRequest(BaseModel):
    connector_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    connection: dict[str, Any] = Field(default_factory=dict)


class PatchConnectionRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    connection: Optional[dict[str, Any]] = None


class JoinStrategyCatalogEntry(BaseModel):
    strategy: str
    mode: str
    scope: str
    freshness: str
    executor: str
    egress: str
    result_mode: str
    caveats: list[str] = Field(default_factory=list)


class SameSourceJoinCatalogEntry(BaseModel):
    strategies: list[JoinStrategyCatalogEntry] = Field(default_factory=list)
    join_types: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class ConnectorCatalogEntry(BaseModel):
    """A connector *type* the platform ships — not a user's saved instance."""

    id: str
    label: str
    category: str
    vendor: str
    maturity: str
    summary: str
    enabled: bool = True
    # Per-connector vocabulary, so the UI stops hardcoding these per provider.
    schema_label: str = "Schema"
    default_schema: str = ""
    relation_noun: str = "table"
    configured_count: int = 0
    same_source_join: Optional[SameSourceJoinCatalogEntry] = None


class ConnectorCatalogResponse(BaseModel):
    connectors: list[ConnectorCatalogEntry] = Field(default_factory=list)


class PinTableRequest(BaseModel):
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    custom_query: Optional[str] = None
    display_name: Optional[str] = None


class AttachTableRequest(BaseModel):
    session_id: Optional[str] = None
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    custom_query: Optional[str] = None


class RemoteTablesResponse(BaseModel):
    schema: str
    tables: list[dict[str, str]]
    collections: list[str] = Field(default_factory=list)


class NamespaceDiscoveryResponse(BaseModel):
    default_namespace: str = ""
    namespaces: list[str] = Field(default_factory=list)
    complete: bool = True
    warning: Optional[str] = None


class ConnectionUsageSession(BaseModel):
    session_id: str
    title: Optional[str] = None
    tables: list[str] = Field(default_factory=list)
    viz_count: int = 0
    filter_count: int = 0


class ConnectionUsageResponse(BaseModel):
    etag: str
    sessions: list[ConnectionUsageSession] = Field(default_factory=list)
    table_count: int = 0
    viz_count: int = 0
    filter_count: int = 0


class ConnectionDeleteResponse(BaseModel):
    ok: bool = True
    detached: list[dict[str, str]] = Field(default_factory=list)


__all__ = [
    "AttachTableRequest",
    "ConnectorCatalogEntry",
    "ConnectorCatalogResponse",
    "ConnectionDeleteResponse",
    "ConnectionUsageResponse",
    "ConnectionUsageSession",
    "CreateConnectionRequest",
    "DatasourceLoadResponse",
    "NamespaceDiscoveryResponse",
    "PatchConnectionRequest",
    "PinTableRequest",
    "RemoteTablesResponse",
    "SavedConnection",
    "SavedConnectionList",
    "SavedTable",
]

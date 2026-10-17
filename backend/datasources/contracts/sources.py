"""Credential-safe source identity models shared by connector implementations."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class AssetRef(BaseModel):
    """An asset inside a connector, without any connection secrets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: Optional[str] = None
    name: str
    asset_type: str = "relation"
    query_id: Optional[str] = None


class SourceRef(BaseModel):
    """A runtime source points to an opaque credential reference, never credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    connector_id: str
    connection_ref: Optional[str] = None
    asset: AssetRef
    schema_fingerprint: Optional[str] = None

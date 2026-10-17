"""Transport-neutral request and result shapes for connector authors."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: Optional[str] = None
    search: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=1000)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)
    columns: tuple[str, ...] = ()
    filters: Dict[str, Any] = Field(default_factory=dict)
    sort_by: Optional[str] = None
    sort_descending: bool = False


class NativeQueryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: Optional[int] = Field(default=None, ge=1)


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    destination_ref: str
    format: str = "parquet"
    row_limit: Optional[int] = Field(default=None, ge=1)


class ConnectionTestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    message: str


class QueryEstimate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bytes_scanned: Optional[int] = None
    rows: Optional[int] = None
    cost: Optional[float] = None
    currency: Optional[str] = None


class QueryHandle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str


class SnapshotResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    location_ref: str
    rows: Optional[int] = None
    bytes_written: Optional[int] = None

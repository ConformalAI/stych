"""Request/response models for the filters API (kept out of api/schemas.py)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FilterPreviewRequest(BaseModel):
    """Dry-run an inline spec against a preview source."""

    spec: Dict[str, Any]
    source: str = Field(..., pattern="^(file|dataset|sql)$")
    path: Optional[str] = None
    file_id: Optional[str] = None
    source_id: Optional[str] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(10, ge=1, le=500)
    matches_only: Optional[bool] = None
    enabled_rules: Optional[List[str]] = None


class FilterSummary(BaseModel):
    matched_rows: int
    total_rows: int
    approx: bool = False
    rule_counts: Dict[str, int] = {}
    group_count: Optional[int] = None
    identity_resolved: str
    degraded: Optional[str] = None
    warning: Optional[str] = None
    annotations: List[Dict[str, Any]] = []
    group_by: Optional[str] = None
    engine: Optional[str] = None
    scope: Optional[str] = None
    freshness: Optional[str] = None
    source_row_limit: Optional[int] = None
    source_truncated: Optional[bool] = None
    schema_complete: Optional[bool] = None


class FilterPage(BaseModel):
    """One page of a filtered source — shape-compatible across both engines."""

    kind: str = "filter"
    columns: List[Dict[str, Any]]
    rows: List[List[Any]]
    cell_meta: List[List[Optional[Dict[str, Any]]]]
    row_meta: List[Optional[Dict[str, Any]]]
    total_rows: int
    filtered_rows: int
    page: int
    total_pages: int
    summary: FilterSummary
    engine: Optional[str] = None
    scope: Optional[str] = None
    freshness: Optional[str] = None
    source_row_limit: Optional[int] = None
    source_truncated: Optional[bool] = None
    schema_complete: Optional[bool] = None


class SavedFilterOut(BaseModel):
    id: str
    session_id: str
    name: str
    dataset_ref: str
    spec: Dict[str, Any]
    version: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SavedFilterList(BaseModel):
    filters: List[SavedFilterOut]


class FilterCreateRequest(BaseModel):
    spec: Dict[str, Any]
    dataset_ref: str = Field(..., min_length=1)
    name: Optional[str] = Field(None, max_length=120)


class FilterUpdateRequest(BaseModel):
    spec: Dict[str, Any]
    name: Optional[str] = Field(None, max_length=120)
    expected_version: Optional[int] = None


class FilterVersionOut(BaseModel):
    version: int
    spec: Dict[str, Any]
    author: str
    created_at: Optional[str] = None


class FilterVersionList(BaseModel):
    versions: List[FilterVersionOut]

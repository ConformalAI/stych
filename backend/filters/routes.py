"""
Filters API: dry-run (preview) an inline FilterSpec against a preview source.

CRUD for saved filters and the saved-filter apply endpoint land with the
persistence layer; this router owns everything under ``/api/filters``.
Sync ``def`` endpoints run in the threadpool (pandas work), matching
``preview_routes.get_preview_diff``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session as DbSession

from agents.state import state_manager
from api import preview_service
from api.dependencies import get_current_user
from api.preview_routes import _adapter_http_error
from api.source_lifecycle import raise_source_removed
from database import filter_store
from database.connection import get_db
from datasources import adapters as table_adapters
from datasources.registry import SOURCE_WORKING_COPY_IDS
from filters import execution as filter_execution
from filters import service, sql_service
from filters.api_models import (
    FilterCreateRequest,
    FilterPage,
    FilterPreviewRequest,
    FilterUpdateRequest,
    FilterVersionList,
    SavedFilterList,
    SavedFilterOut,
)
from filters.predicate_pandas import UnknownColumn
from filters.spec import FilterSpec, parse_spec, validation_errors

router = APIRouter(prefix="/filters", tags=["filters"])

# Module-level indirection so tests can inject a fake adapter resolver
# (same pattern as api/preview_routes).
_resolve_sql_adapter = table_adapters.resolve_adapter


def _get_session(session_id: str, user: str):
    session = state_manager.get_session(session_id, app_user_id=user)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _filter_or_404(db: DbSession, filter_id: str, user: str):
    try:
        row = filter_store.get_filter(db, filter_id, user)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Filter belongs to a different user")
    if row is None:
        raise HTTPException(status_code=404, detail="Filter not found")
    return row


def _active_sql_variant(ctx: Any) -> str:
    """Route dataset-level filters to what the Data tab currently displays."""
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415

    meta = getattr(ctx, "metadata", None) or {}
    return wc.active_source_variant(meta)


def _dataset_ref_aliases(session: Any, dataset_ref: str) -> list[str]:
    """Include the legacy ``::temp`` alias only for the active named copy."""
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415

    file_id, sep, variant = dataset_ref.rpartition("::")
    if not sep:
        return [dataset_ref]
    ctx = next(
        (item for item in session.get_all_data_contexts() if item.file_id == file_id),
        None,
    )
    if ctx is None:
        return [dataset_ref]
    canonical = wc.active_source_variant(getattr(ctx, "metadata", None) or {})
    if canonical.startswith("copy:") and variant in (canonical, "temp"):
        return [f"{file_id}::{canonical}", f"{file_id}::temp"]
    return [dataset_ref]


def apply_spec(
    session,
    session_id: str,
    user: str,
    spec: FilterSpec,
    *,
    source: str,
    path: Optional[str] = None,
    file_id: Optional[str] = None,
    source_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
    matches_only: Optional[bool] = None,
    enabled_rules: Optional[List[str]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Shared apply path for the preview and saved-filter endpoints."""
    if source == "sql":
        ctx, variant = preview_service.resolve_sql_source_id(session, source_id)
        source = (ctx.metadata or {}).get("source")
        if source not in SOURCE_WORKING_COPY_IDS:
            if variant not in ("base", "working"):
                raise HTTPException(
                    status_code=400,
                    detail="Named working copies are supported for PostgreSQL and Databricks only.",
                )
            return filter_execution.build_duckdb_filter_page(
                session,
                ctx,
                spec,
                session_id=session_id,
                user=user,
                page=page,
                page_size=page_size,
                matches_only=matches_only,
                enabled_rules=enabled_rules,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=filters,
            )
        if source == "mongodb":
            # Documents are filtered by aggregation, not SQL — the SQL service
            # below has no compilation target for a collection.
            return filter_execution.build_mongo_filter_page(
                ctx,
                spec,
                variant,
                session_id=session_id,
                page=page,
                page_size=page_size,
                matches_only=matches_only,
                enabled_rules=enabled_rules,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=filters,
            )
        try:
            adapter = _resolve_sql_adapter(ctx, variant)
            return sql_service.build_sql_filter_page(
                adapter,
                spec,
                session_id=session_id,
                page=page,
                page_size=page_size,
                matches_only=matches_only,
                enabled_rules=enabled_rules,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=filters,
            )
        except UnknownColumn as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Select predicate references unknown column {exc.column!r}.",
            )
        except service.AnnotationCollision as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except table_adapters.AdapterError as exc:
            raise _adapter_http_error(exc)

    if source == "dataset":
        ctx = next(
            (item for item in session.get_all_data_contexts() if item.file_id == file_id),
            None,
        )
        if ctx is None:
            raise_source_removed()
        origin = (ctx.metadata or {}).get("source")
        remote = callable(getattr(ctx, "is_remote_source", None)) and ctx.is_remote_source()
        if remote or origin in SOURCE_WORKING_COPY_IDS:
            variant = _active_sql_variant(ctx)
            return apply_spec(
                session,
                session_id,
                user,
                spec,
                source="sql",
                source_id=f"{ctx.file_id}::{variant}",
                page=page,
                page_size=page_size,
                matches_only=matches_only,
                enabled_rules=enabled_rules,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                filters=filters,
            )
        return filter_execution.build_duckdb_filter_page(
            session,
            ctx,
            spec,
            session_id=session_id,
            user=user,
            page=page,
            page_size=page_size,
            matches_only=matches_only,
            enabled_rules=enabled_rules,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
            filters=filters,
        )

    df, cache_part = preview_service.load_tabular_source(
        session, session_id, user, source, path, file_id
    )
    if df is None:
        raise_source_removed()

    return filter_execution.build_pandas_filter_page(
        df,
        cache_part,
        spec,
        session_id=session_id,
        user=user,
        page=page,
        page_size=page_size,
        matches_only=matches_only,
        enabled_rules=enabled_rules,
        sort_by=sort_by,
        sort_dir=sort_dir,
        search=search,
        filters=filters,
    )


@router.post("/sessions/{session_id}/preview", response_model=FilterPage)
def preview_filter(
    session_id: str,
    body: FilterPreviewRequest,
    user: str = Depends(get_current_user),
):
    """Dry-run an inline spec: used by the agent's preview tool and the UI."""
    session = _get_session(session_id, user)
    try:
        spec = parse_spec(body.spec)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"errors": validation_errors(exc)})
    return apply_spec(
        session,
        session_id,
        user,
        spec,
        source=body.source,
        path=body.path,
        file_id=body.file_id,
        source_id=body.source_id,
        page=body.page,
        page_size=body.page_size,
        matches_only=body.matches_only,
        enabled_rules=body.enabled_rules,
    )


# ------------------------------------------------------------------ saved-filter apply


def _parse_rules(rules: Optional[str]) -> Optional[List[str]]:
    if not rules:
        return None
    try:
        parsed = json.loads(rules)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid rules parameter.")
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail="Invalid rules parameter.")
    return [str(r) for r in parsed]


@router.get("/sessions/{session_id}/apply", response_model=FilterPage)
def apply_saved_filter(
    session_id: str,
    filter_id: str = Query(...),
    source: str = Query(..., pattern="^(file|dataset|sql)$"),
    path: Optional[str] = Query(None),
    file_id: Optional[str] = Query(None),
    source_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    matches_only: Optional[bool] = Query(None),
    rules: Optional[str] = Query(None, description="JSON array of enabled rule ids"),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    search: Optional[str] = Query(None),
    filters: Optional[str] = Query(None, description='JSON object {"col": "substring"}'),
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """Serve one page of a source through a saved filter (the grid provider)."""
    session = _get_session(session_id, user)
    row = _filter_or_404(db, filter_id, user)
    try:
        spec = parse_spec(json.loads(row.spec_json))
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"errors": validation_errors(exc)})
    return apply_spec(
        session,
        session_id,
        user,
        spec,
        source=source,
        path=path,
        file_id=file_id,
        source_id=source_id,
        page=page,
        page_size=page_size,
        matches_only=matches_only,
        enabled_rules=_parse_rules(rules),
        sort_by=sort_by,
        sort_dir=sort_dir,
        search=search,
        filters=preview_service.parse_filters_param(filters),
    )


# ------------------------------------------------------------------ CRUD


@router.get("/sessions/{session_id}", response_model=SavedFilterList)
def list_saved_filters(
    session_id: str,
    dataset_ref: Optional[str] = Query(None),
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    aliases = None
    if dataset_ref:
        session = state_manager.get_session(session_id, app_user_id=user)
        if session is not None:
            aliases = _dataset_ref_aliases(session, dataset_ref)
    rows = filter_store.list_filters(
        db,
        session_id,
        user,
        dataset_ref=dataset_ref,
        dataset_ref_aliases=aliases,
    )
    return {"filters": [filter_store.filter_to_dict(r) for r in rows]}


@router.post("/sessions/{session_id}", response_model=SavedFilterOut, status_code=201)
def create_saved_filter(
    session_id: str,
    body: FilterCreateRequest,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    try:
        spec = parse_spec(body.spec)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"errors": validation_errors(exc)})
    row = filter_store.create_filter(
        db,
        session_id=session_id,
        app_user_id=user,
        spec=spec.model_dump(mode="json"),
        dataset_ref=body.dataset_ref,
        name=body.name,
        author="user",
    )
    db.commit()
    return filter_store.filter_to_dict(row)


@router.get("/{filter_id}", response_model=SavedFilterOut)
def get_saved_filter(
    filter_id: str,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    return filter_store.filter_to_dict(_filter_or_404(db, filter_id, user))


@router.put("/{filter_id}", response_model=SavedFilterOut)
def update_saved_filter(
    filter_id: str,
    body: FilterUpdateRequest,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    _filter_or_404(db, filter_id, user)
    try:
        spec = parse_spec(body.spec)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"errors": validation_errors(exc)})
    try:
        row = filter_store.update_filter(
            db,
            filter_id,
            user,
            spec=spec.model_dump(mode="json"),
            name=body.name,
            author="user",
            expected_version=body.expected_version,
        )
    except filter_store.VersionConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": str(exc),
                "expected_version": exc.expected,
                "current_version": exc.current,
            },
        ) from exc
    db.commit()
    return filter_store.filter_to_dict(row)


@router.delete("/{filter_id}", status_code=204)
def delete_saved_filter(
    filter_id: str,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    _filter_or_404(db, filter_id, user)
    filter_store.soft_delete_filter(db, filter_id, user)
    db.commit()


@router.get("/{filter_id}/versions", response_model=FilterVersionList)
def list_saved_filter_versions(
    filter_id: str,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    _filter_or_404(db, filter_id, user)
    rows = filter_store.list_versions(db, filter_id, user)
    return {"versions": [filter_store.version_to_dict(r) for r in rows]}


@router.post("/{filter_id}/restore/{version_n}", response_model=SavedFilterOut)
def restore_saved_filter_version(
    filter_id: str,
    version_n: int,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    _filter_or_404(db, filter_id, user)
    row = filter_store.restore_version(db, filter_id, user, version_n)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    db.commit()
    return filter_store.filter_to_dict(row)

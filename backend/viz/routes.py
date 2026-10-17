"""
REST API for the visualization subsystem.

CRUD over persisted visualizations (spec-only — data is always re-materialized),
the ad-hoc ``/query`` endpoint that powers the Visualize tab's editing loop, and
the field catalog. Spec problems return 422 with structured ``errors``
(path/code/message), the same shape the agent tools consume.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from agents.state import state_manager
from api.dependencies import get_config, get_current_user, get_current_user_record, get_orchestrator
from api.source_lifecycle import SOURCE_REMOVED_DETAIL, SOURCE_REMOVED_STATUS, raise_source_removed
from database import viz_store
from database.connection import get_db
from llm.langfuse_client import get_langfuse_tracker
from llm.usage_limit import (
    LIMIT_REACHED_MESSAGE,
    UsageLimitReached,
    over_limit_message_or_none,
    reset_usage_limit_context,
    set_usage_limit_context,
)
from llm.usage_tracker import get_usage_tracker

from .catalog import get_field_catalog
from .executor import (
    SourceUnavailableError,
    SpecValidationError,
    materialize_spec,
    resolve_data_context,
)
from .expr import calc_field_info, resolve_calculations
from .spec import CalculateTransform, VizSpec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/viz", tags=["viz"])

_viz_request_tasks: Dict[str, set[asyncio.Task]] = {}
_MAX_CONCURRENT_VIZ_REQUESTS_PER_USER = max(
    1, int(os.getenv("MAX_CONCURRENT_VIZ_REQUESTS_PER_USER", "3"))
)


class QueryRequest(BaseModel):
    spec: Dict[str, Any]


class CalcValidateRequest(BaseModel):
    """The spec's transform list (filter entries are ignored)."""

    transform: List[Dict[str, Any]]
    source: str = "active"


class AskRequest(BaseModel):
    """'Ask this chart': a spec, a selection on it, and what the user wants."""

    spec: Dict[str, Any]
    selection: Dict[str, Any]
    mode: str = "explain"  # explain | compare | drill | custom
    question: Optional[str] = None
    viz_id: Optional[str] = None  # provenance only; the spec in the body is used


class SaveRequest(BaseModel):
    spec: Dict[str, Any]
    title: Optional[str] = None
    expected_version: Optional[int] = None


def _get_state(session_id: str, user: str, *, require_dataset: bool = True):
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if require_dataset and session.data_context is None:
        raise_source_removed()
    return session


def _acquire_viz_request_slot(user: str) -> asyncio.Task:
    """Limit expensive materialization and chart-analysis work per user."""
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - FastAPI requests always run in a task
        raise RuntimeError("Visualization request must run in an asyncio task")
    active_tasks = {item for item in _viz_request_tasks.get(user, set()) if not item.done()}
    _viz_request_tasks[user] = active_tasks
    if len(active_tasks) >= _MAX_CONCURRENT_VIZ_REQUESTS_PER_USER:
        raise HTTPException(
            status_code=429,
            detail="Too many visualization requests. Please wait for one to finish.",
        )
    active_tasks.add(task)
    return task


def _release_viz_request_slot(user: str, task: asyncio.Task) -> None:
    _viz_request_tasks.get(user, set()).discard(task)


def _pydantic_errors(exc: ValidationError) -> List[dict]:
    return [
        {
            "path": ".".join(str(p) for p in e.get("loc", [])),
            "code": e.get("type", "invalid"),
            "message": e.get("msg", "Invalid value"),
        }
        for e in exc.errors()
    ]


def _parse_spec(spec_dict: Dict[str, Any]) -> VizSpec:
    try:
        return VizSpec.model_validate(spec_dict)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": _pydantic_errors(exc)}) from exc


async def _materialize(
    state,
    spec_dict: Dict[str, Any],
    sql_agent,
    *,
    source_hint: Optional[str] = None,
):
    spec = _parse_spec(spec_dict)
    try:
        normalized, data = await materialize_spec(
            state,
            spec,
            sql_agent=sql_agent,
            source_hint=source_hint,
        )
    except SpecValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.to_payload()}) from exc
    except SourceUnavailableError as exc:
        raise HTTPException(
            status_code=SOURCE_REMOVED_STATUS, detail=SOURCE_REMOVED_DETAIL
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return normalized, data


def _viz_or_404(db: Session, viz_id: str, user: str):
    try:
        row = viz_store.get_visualization(db, viz_id, user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Visualization not found")
    return row


def _raise_version_conflict(exc: viz_store.VersionConflictError) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "version_conflict",
            "message": str(exc),
            "expected_version": exc.expected,
            "current_version": exc.current,
        },
    ) from exc


def _with_source_status(payload: dict, state: Any) -> dict:
    spec_source = str(((payload.get("spec") or {}).get("data") or {}).get("source") or "active")
    hint = payload.get("dataset_ref") if spec_source == "active" else None
    try:
        resolve_data_context(state, spec_source, source_hint=hint)
        available = True
    except SourceUnavailableError:
        available = False
    return {**payload, "source_available": available}


# ---------------------------------------------------------------------------
# Catalog + ad-hoc query (the UI editing loop)
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/catalog")
async def get_catalog(
    session_id: str,
    source: Optional[str] = None,
    user: str = Depends(get_current_user),
    orch=Depends(get_orchestrator),
):
    state = _get_state(session_id, user)
    try:
        dc = resolve_data_context(state, source or "active")
        catalog = await get_field_catalog(
            state,
            sql_agent=getattr(orch, "sql_agent", None),
            data_context=dc,
        )
    except SourceUnavailableError:
        raise_source_removed()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return catalog.model_dump(mode="json")


@router.post("/sessions/{session_id}/query")
async def query_spec(
    session_id: str,
    body: QueryRequest,
    user: str = Depends(get_current_user),
    orch=Depends(get_orchestrator),
):
    task = _acquire_viz_request_slot(user)
    try:
        state = _get_state(session_id, user)
        normalized, data = await _materialize(state, body.spec, getattr(orch, "sql_agent", None))
        return {"spec": normalized.to_json_dict(), "data": data.model_dump(by_alias=True)}
    finally:
        _release_viz_request_slot(user, task)


@router.post("/sessions/{session_id}/calculations/validate")
async def validate_calculations(
    session_id: str,
    body: CalcValidateRequest,
    user: str = Depends(get_current_user),
    orch=Depends(get_orchestrator),
):
    """
    Parse + type-check calculated fields without executing anything — powers the
    calculated-field editor's instant feedback. Always 200; problems come back
    in ``errors`` (same path/code/message shape as spec validation). ``errors``
    paths index into the calculate entries in THEIR original transform order.
    """
    state = _get_state(session_id, user)
    try:
        dc = resolve_data_context(state, body.source)
        catalog = await get_field_catalog(
            state,
            sql_agent=getattr(orch, "sql_agent", None),
            data_context=dc,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    calcs: List[CalculateTransform] = []
    calc_indices: List[int] = []
    errors: List[dict] = []
    for i, entry in enumerate(body.transform):
        if not isinstance(entry, dict) or entry.get("op") != "calculate":
            continue
        try:
            calcs.append(CalculateTransform.model_validate(entry))
            calc_indices.append(i)
        except ValidationError as exc:
            errors.extend(
                {**e, "path": f"transform[{i}].{e['path']}"} for e in _pydantic_errors(exc)
            )
    resolved, calc_errors = resolve_calculations(calcs, catalog, transform_indices=calc_indices)
    errors.extend(e.model_dump() for e in calc_errors)
    return {
        "fields": [calc_field_info(c).model_dump(mode="json") for c in resolved],
        "errors": errors,
    }


@router.post("/sessions/{session_id}/ask")
async def ask_chart_selection(
    session_id: str,
    body: AskRequest,
    user: str = Depends(get_current_user),
    user_record=Depends(get_current_user_record),
    config=Depends(get_config),
    orch=Depends(get_orchestrator),
):
    """
    Analyze a chart selection: deterministic selected-vs-rest stats + driver
    analysis, narrated by one LLM call; may persist an agent-authored
    drill-down visualization (returned as ``drill.viz_id``).
    """
    from .inquiry import ask_selection  # noqa: PLC0415

    task = _acquire_viz_request_slot(user)
    usage_token = set_usage_limit_context(
        config,
        user,
        user_record.email if user_record else None,
    )
    try:
        quota_ok, quota_reason = get_usage_tracker().is_within_quota(user_id=user)
        if not quota_ok:
            raise HTTPException(status_code=402, detail=quota_reason or "Usage limit reached")
        limit_message = over_limit_message_or_none()
        if limit_message is not None:
            raise HTTPException(status_code=402, detail=limit_message)

        tracker = get_langfuse_tracker()
        if tracker:
            tracker.set_session_id(session_id)
            tracker.set_user_id(user)

        state = _get_state(session_id, user)
        return await ask_selection(
            state,
            body.spec,
            body.selection,
            body.mode,
            body.question,
            graph_agent=orch.graph_agent,
            sql_agent=getattr(orch, "sql_agent", None),
            user_id=user,
        )
    except SpecValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.to_payload()}) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": _pydantic_errors(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UsageLimitReached as exc:
        raise HTTPException(status_code=402, detail=LIMIT_REACHED_MESSAGE(exc.limit)) from exc
    finally:
        _release_viz_request_slot(user, task)
        reset_usage_limit_context(usage_token)


# ---------------------------------------------------------------------------
# CRUD + versions
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/visualizations")
async def list_visualizations(
    session_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = viz_store.list_visualizations(db, session_id, user)
    state = _get_state(session_id, user, require_dataset=False)
    return {
        "visualizations": [_with_source_status(viz_store.viz_to_dict(row), state) for row in rows]
    }


@router.post("/sessions/{session_id}/visualizations", status_code=201)
async def create_visualization(
    session_id: str,
    body: SaveRequest,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch=Depends(get_orchestrator),
):
    state = _get_state(session_id, user)
    normalized, data = await _materialize(state, body.spec, getattr(orch, "sql_agent", None))
    row = viz_store.create_visualization(
        db,
        session_id=session_id,
        app_user_id=user,
        spec=normalized.to_json_dict(),
        title=body.title,
        dataset_ref=normalized.data.source,
        author="user",
    )
    db.commit()
    return {"visualization": viz_store.viz_to_dict(row), "data": data.model_dump(by_alias=True)}


@router.get("/visualizations/{viz_id}")
async def get_visualization(
    viz_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _viz_or_404(db, viz_id, user)
    state = _get_state(row.session_history_id, user, require_dataset=False)
    return {"visualization": _with_source_status(viz_store.viz_to_dict(row), state)}


@router.put("/visualizations/{viz_id}")
async def update_visualization(
    viz_id: str,
    body: SaveRequest,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch=Depends(get_orchestrator),
):
    row = _viz_or_404(db, viz_id, user)
    state = _get_state(row.session_history_id, user, require_dataset=False)
    normalized, data = await _materialize(
        state,
        body.spec,
        getattr(orch, "sql_agent", None),
        source_hint=row.dataset_ref,
    )
    try:
        row = viz_store.update_visualization(
            db,
            viz_id,
            user,
            spec=normalized.to_json_dict(),
            title=body.title,
            dataset_ref=normalized.data.source,
            author="user",
            expected_version=body.expected_version,
        )
    except viz_store.VersionConflictError as exc:
        db.rollback()
        _raise_version_conflict(exc)
    db.commit()
    return {"visualization": viz_store.viz_to_dict(row), "data": data.model_dump(by_alias=True)}


@router.delete("/visualizations/{viz_id}")
async def delete_visualization(
    viz_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _viz_or_404(db, viz_id, user)
    viz_store.soft_delete_visualization(db, viz_id, user)
    db.commit()
    return {"deleted": True}


@router.get("/visualizations/{viz_id}/data")
async def get_visualization_data(
    viz_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch=Depends(get_orchestrator),
):
    row = _viz_or_404(db, viz_id, user)
    state = _get_state(row.session_history_id, user, require_dataset=False)
    spec_dict = viz_store.viz_to_dict(row)["spec"]
    normalized, data = await _materialize(
        state,
        spec_dict,
        getattr(orch, "sql_agent", None),
        source_hint=row.dataset_ref,
    )
    return {"spec": normalized.to_json_dict(), "data": data.model_dump(by_alias=True)}


@router.get("/visualizations/{viz_id}/versions")
async def get_versions(
    viz_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _viz_or_404(db, viz_id, user)
    rows = viz_store.list_versions(db, viz_id, user)
    return {"versions": [viz_store.version_to_dict(r) for r in rows]}


@router.post("/visualizations/{viz_id}/restore/{version_n}")
async def restore_version(
    viz_id: str,
    version_n: int,
    expected_version: Optional[int] = None,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _viz_or_404(db, viz_id, user)
    try:
        row = viz_store.restore_version(
            db,
            viz_id,
            user,
            version_n,
            author="user",
            expected_version=expected_version,
        )
    except viz_store.VersionConflictError as exc:
        db.rollback()
        _raise_version_conflict(exc)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    db.commit()
    return {"visualization": viz_store.viz_to_dict(row)}

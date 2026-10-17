"""
REST API for the ontology subsystem.

One ontology per session, so there is no create/list pair — ``GET`` seeds an
empty spec on first access. Everything else mirrors ``viz/routes.py``: spec
problems return 422 with structured ``errors`` (path/code/message), concurrent
edits return 409, and data is always re-materialized, never persisted.

The proposal endpoints are what the chat card drives: accepting certifies
exactly the names the agent added, rejecting restores the parent version.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from agents.state import state_manager
from api.dependencies import get_current_user, get_orchestrator
from database import ontology_store
from database.connection import get_db

from .executor import (
    OntologyValidationError,
    catalogs_for_spec,
    dataset_labels,
    missing_datasets,
    preview_query,
)
from .resolve import QueryDimension
from .spec import OntologySpec
from .validate import normalize_spec, validate_spec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ontology", tags=["ontology"])

MAX_QUERY_METRICS = 10
MAX_QUERY_DIMENSIONS = 4


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SaveRequest(BaseModel):
    spec: Dict[str, Any]
    title: Optional[str] = None
    expected_version: Optional[int] = None
    summary: Optional[str] = None


class CertifyRequest(BaseModel):
    names: List[str]
    certified: bool = True


class QueryDimensionRequest(BaseModel):
    name: str
    time_unit: Optional[str] = None
    top_k: Optional[int] = None


class QueryRequest(BaseModel):
    metrics: List[str] = []
    dimensions: List[QueryDimensionRequest] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_state(session_id: str, user: str, *, required: bool = True):
    """The session's in-memory AgentState, or None on read paths without one.

    A session that has never had an upload has no ``AgentState`` — ``get_session``
    only returns existing ones, it does not create them. The ontology is a
    DB-backed artifact and does not need session state to be *read*; it needs it
    only to enrich the response with dataset catalogs. So reads tolerate its
    absence instead of 404-ing, which is what made the Ontology tab unopenable on
    a fresh session. Writes still require it — there is nothing to define an
    ontology over without data.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session and required:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _parse_spec(spec_dict: Dict[str, Any]) -> OntologySpec:
    try:
        return OntologySpec.model_validate(spec_dict)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": _pydantic_errors(exc)}) from exc


def _pydantic_errors(exc: ValidationError) -> List[dict]:
    return [
        {
            "path": ".".join(str(p) for p in e.get("loc", [])),
            "code": e.get("type", "invalid"),
            "message": e.get("msg", "Invalid value"),
        }
        for e in exc.errors()
    ]


def _row_or_404(db: Session, ontology_id: str, user: str):
    try:
        row = ontology_store.get_ontology(db, ontology_id, user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Not your ontology") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Ontology not found")
    return row


def _raise_conflict(exc: ontology_store.VersionConflictError) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "version_conflict",
            "message": str(exc),
            "expected_version": exc.expected,
            "current_version": exc.current,
        },
    ) from exc


async def _session_datasets(state: Any, sql_agent: Any) -> List[dict]:
    """Every dataset in the session with its columns — the entity palette."""
    from viz.catalog import get_field_catalog

    from .executor import _all_data_contexts, _scoped_state

    out: List[dict] = []
    for dc in _all_data_contexts(state):
        if dc is None:
            continue
        entry = {
            "id": str(dc.file_id),
            "filename": str(dc.filename),
            "fields": [],
            "row_count": None,
        }
        try:
            catalog = await get_field_catalog(_scoped_state(state, dc), sql_agent)
            entry["fields"] = [f.model_dump(mode="json") for f in catalog.fields]
            entry["row_count"] = catalog.row_count
        except Exception as exc:  # noqa: BLE001 — a listing must not 500
            logger.warning("[ontology] catalog failed for %s: %s", dc.file_id, exc)
        out.append(entry)
    return out


async def _payload(db: Session, row, state: Any, sql_agent: Any) -> dict:
    """The full editor payload: spec, its errors, and the datasets it can use."""
    spec = _parse_spec(json.loads(row.spec_json))
    if state is None:
        # No session state yet, so there are no datasets to check against.
        # Reporting "unknown dataset" for every entity here would be a lie about
        # the ontology rather than a fact about the session.
        return {
            "id": row.id,
            "title": row.title,
            "spec": spec.to_json_dict(),
            "version": row.version,
            "errors": [],
            "datasets": [],
            "missing_datasets": [],
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    catalogs = await catalogs_for_spec(state, spec, sql_agent)
    normalized = normalize_spec(spec, catalogs, dataset_labels(state, spec))
    errors = validate_spec(normalized, catalogs)
    return {
        "id": row.id,
        "title": row.title,
        "spec": normalized.to_json_dict(),
        "version": row.version,
        "errors": [e.model_dump() for e in errors],
        "datasets": await _session_datasets(state, sql_agent),
        "missing_datasets": missing_datasets(state, normalized),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _set_certified(
    spec: OntologySpec, names: List[str], certified: bool
) -> tuple[OntologySpec, int]:
    """Flip the certified flag on every definition named, whatever its kind."""
    wanted = set(names)
    changed = 0
    for group in (spec.entities, spec.relationships, spec.dimensions, spec.metrics):
        for item in group:
            if item.name in wanted and item.certified != certified:
                item.certified = certified
                changed += 1
    return spec, changed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def get_ontology(
    session_id: Optional[str] = None,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Any = Depends(get_orchestrator),
):
    """The user's ontology.

    ``session_id`` is optional validation context, not identity: it decides
    which datasets the spec is checked against and what ``missing_datasets``
    reports. Without it the spec comes back unvalidated rather than flagged as
    broken, because an ontology that outlives a session is not wrong just
    because that session's data is not loaded.
    """
    state = _get_state(session_id, user, required=False) if session_id else None
    row = ontology_store.get_or_create(db, user)
    return await _payload(db, row, state, getattr(orch, "sql_agent", None))


@router.put("")
async def save_ontology(
    body: SaveRequest,
    session_id: Optional[str] = None,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Any = Depends(get_orchestrator),
):
    state = _get_state(session_id, user, required=False) if session_id else None
    sql_agent = getattr(orch, "sql_agent", None)
    spec = _parse_spec(body.spec)

    if state is None:
        normalized, errors = spec, []
    else:
        catalogs = await catalogs_for_spec(state, spec, sql_agent)
        normalized = normalize_spec(spec, catalogs, dataset_labels(state, spec))
        errors = validate_spec(normalized, catalogs)
    # Errors are returned WITH the save, not instead of it: a half-built
    # ontology must be saveable, or the editor cannot hold work in progress.
    row = ontology_store.get_or_create(db, user)
    try:
        row = ontology_store.save_spec(
            db,
            row.id,
            user,
            normalized.to_json_dict(),
            expected_version=body.expected_version,
            title=body.title,
            author="user",
            summary=body.summary,
        )
    except ontology_store.VersionConflictError as exc:
        _raise_conflict(exc)

    return {
        "id": row.id,
        "spec": json.loads(row.spec_json),
        "version": row.version,
        "errors": [e.model_dump() for e in errors],
    }


@router.post("/certify")
async def certify(
    body: CertifyRequest,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = ontology_store.get_or_create(db, user)
    spec = _parse_spec(json.loads(row.spec_json))
    spec, changed = _set_certified(spec, body.names, body.certified)
    if changed == 0:
        return {"id": row.id, "spec": spec.to_json_dict(), "version": row.version, "changed": 0}

    verb = "certified" if body.certified else "uncertified"
    row = ontology_store.save_spec(
        db,
        row.id,
        user,
        spec.to_json_dict(),
        author="user",
        summary=f"{verb} {', '.join(sorted(set(body.names))[:5])}",
    )
    return {
        "id": row.id,
        "spec": json.loads(row.spec_json),
        "version": row.version,
        "changed": changed,
    }


@router.post("/sessions/{session_id}/query")
async def query(
    session_id: str,
    body: QueryRequest,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Any = Depends(get_orchestrator),
):
    """Run a semantic query. Powers the Ontology tab's metric preview."""
    if len(body.metrics) > MAX_QUERY_METRICS:
        raise HTTPException(status_code=422, detail="Too many metrics in one query")
    if len(body.dimensions) > MAX_QUERY_DIMENSIONS:
        raise HTTPException(status_code=422, detail="Too many dimensions in one query")

    state = _get_state(session_id, user)
    row = ontology_store.get_or_create(db, user)
    spec = _parse_spec(json.loads(row.spec_json))
    dimensions = [
        QueryDimension(name=d.name, time_unit=d.time_unit, top_k=d.top_k) for d in body.dimensions
    ]
    try:
        resolved, data = await preview_query(
            state, spec, body.metrics, dimensions, sql_agent=getattr(orch, "sql_agent", None)
        )
    except OntologyValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.to_payload()}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "data": data.model_dump(by_alias=True, mode="json"),
        "uncertified": resolved.uncertified,
        "spans_entities": resolved.spans_entities,
        "fan_out": resolved.fan_out_relationships,
        "base_entity": resolved.base_entity.name,
    }


@router.get("/{ontology_id}/versions")
async def list_versions(
    ontology_id: str,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _row_or_404(db, ontology_id, user)
    rows = ontology_store.list_versions(db, ontology_id, user)
    return {"versions": [ontology_store.version_to_dict(r) for r in rows]}


@router.post("/{ontology_id}/restore/{version_n}")
async def restore(
    ontology_id: str,
    version_n: int,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _row_or_404(db, ontology_id, user)
    try:
        row = ontology_store.restore_version(db, ontology_id, user, version_n)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "spec": json.loads(row.spec_json), "version": row.version}


@router.post("/{ontology_id}/proposals/{version_n}/accept")
async def accept_proposal(
    ontology_id: str,
    version_n: int,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Certify exactly the definitions the agent added in this version."""
    row = _row_or_404(db, ontology_id, user)
    version = ontology_store.get_version(db, ontology_id, user, version_n)
    if version is None:
        raise HTTPException(status_code=404, detail=f"Version {version_n} not found")

    names = json.loads(version.proposed_json) if version.proposed_json else []
    spec = _parse_spec(json.loads(row.spec_json))
    spec, changed = _set_certified(spec, names, True)
    if changed:
        row = ontology_store.save_spec(
            db,
            ontology_id,
            user,
            spec.to_json_dict(),
            author="user",
            summary=f"accepted {len(names)} proposed definition(s)",
        )
    return {
        "id": row.id,
        "spec": json.loads(row.spec_json),
        "version": row.version,
        "accepted": names,
    }


@router.post("/{ontology_id}/proposals/{version_n}/reject")
async def reject_proposal(
    ontology_id: str,
    version_n: int,
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Undo a proposal by restoring the version it was built on."""
    _row_or_404(db, ontology_id, user)
    if version_n <= 1:
        raise HTTPException(status_code=422, detail="Nothing to roll back to")
    try:
        row = ontology_store.restore_version(db, ontology_id, user, version_n - 1)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": row.id, "spec": json.loads(row.spec_json), "version": row.version}

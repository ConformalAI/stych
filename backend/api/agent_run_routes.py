"""Detached, in-process agent-run API.

Runs belong to the backend process rather than an individual HTTP response.
SSE viewers can disconnect and reconnect without cancelling execution.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from agents.run_executor import execute_agent_run
from agents.run_manager import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ActiveSessionRunError,
    AgentRun,
    ConcurrentRunLimitError,
    agent_run_registry,
)
from agents.state import state_manager
from api.dependencies import (
    get_config,
    get_current_user,
    get_current_user_record,
    get_orchestrator,
    resolve_session,
)
from api.schemas import QueryRequest
from helpers.sse import SSE_HEARTBEAT_INTERVAL, SseEventType
from helpers.validators import sanitize_input
from llm.usage_tracker import get_usage_tracker

router = APIRouter(prefix="/api/query/runs", tags=["agent-runs"])

_MAX_CONCURRENT_RUNS_PER_USER = max(1, int(os.getenv("MAX_CONCURRENT_STREAMS_PER_USER", "3")))


async def _agent_run_event_generator(run: AgentRun):
    """Attach to a run without taking ownership of its execution task."""
    snapshot = await run.events.snapshot(run)
    sequence = int(snapshot["sequence"])
    yield "data: " + json.dumps(snapshot, default=str) + "\n\n"
    if run.status in TERMINAL_RUN_STATUSES:
        return

    while True:
        events, status = await run.events.wait_after(
            sequence,
            run,
            SSE_HEARTBEAT_INTERVAL,
        )
        if not events:
            if status in TERMINAL_RUN_STATUSES:
                break
            yield "data: " + json.dumps(
                {"type": SseEventType.HEARTBEAT.value, "run_id": run.id}
            ) + "\n\n"
            continue
        for event in events:
            sequence = max(sequence, int(event.get("sequence") or sequence))
            yield "data: " + json.dumps(event, default=str) + "\n\n"
        if status in TERMINAL_RUN_STATUSES:
            break


@router.post("")
async def create_query_run(
    request: QueryRequest,
    orchestrator=Depends(get_orchestrator),
    user: str = Depends(get_current_user),
    user_record=Depends(get_current_user_record),
    config=Depends(get_config),
):
    quota_ok, quota_reason = get_usage_tracker().is_within_quota(user_id=user)
    if not quota_ok:
        raise HTTPException(status_code=402, detail=quota_reason or "Usage limit reached")

    session = resolve_session(request.session_id, user, is_anonymous=request.is_anonymous)
    session.metadata = session.metadata or {}
    session.metadata["long_term_memory_enabled"] = bool(
        getattr(user_record, "long_term_memory_enabled", True)
    )
    try:
        run, created = await agent_run_registry.admit(
            request.session_id,
            user,
            request.client_request_id,
            max_active=_MAX_CONCURRENT_RUNS_PER_USER,
        )
    except ActiveSessionRunError as exc:
        raise HTTPException(
            status_code=409, detail="This session already has a query running."
        ) from exc
    except ConcurrentRunLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent queries. Please wait for a previous one to finish.",
        ) from exc
    if not created:
        return run.public_dict()

    message = sanitize_input(request.message)
    if message != "__user_action__":
        session.add_message("user", message, metadata={"run_id": run.id})

    effective_model = request.model_id or (user_record.preferred_model if user_record else None)
    run.task = asyncio.create_task(
        execute_agent_run(
            run,
            request,
            orchestrator=orchestrator,
            session=session,
            config=config,
            user_id=user,
            user_email=user_record.email if user_record else None,
            effective_model=effective_model,
        )
    )
    return run.public_dict()


@router.get("/session/{session_id}")
async def get_session_query_run(
    session_id: str,
    user: str = Depends(get_current_user),
):
    if state_manager.get_session(session_id, app_user_id=user) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    run = agent_run_registry.latest_for_session(session_id, user)
    return {"run": run.public_dict() if run is not None else None}


@router.get("/{run_id}/stream")
async def stream_query_run(
    run_id: str,
    user: str = Depends(get_current_user),
):
    run = agent_run_registry.get(run_id, user)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(
        _agent_run_event_generator(run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{run_id}/cancel")
async def cancel_query_run(
    run_id: str,
    user: str = Depends(get_current_user),
):
    run = agent_run_registry.get(run_id, user)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ACTIVE_RUN_STATUSES:
        return run.public_dict()
    run.cancel_requested = True
    if run.task is not None and not run.task.done():
        run.task.cancel()
    return run.public_dict()

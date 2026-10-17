from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.run_executor import execute_agent_run
from agents.run_manager import ActiveSessionRunError, AgentRun, AgentRunRegistry
from api.agent_run_routes import _agent_run_event_generator
from api.dependencies import (
    get_config,
    get_current_user,
    get_current_user_record,
    get_orchestrator,
)


async def test_run_stream_disconnect_does_not_cancel_worker():
    release = asyncio.Event()

    async def worker() -> None:
        await release.wait()

    task = asyncio.create_task(worker())
    run = AgentRun(id="run-1", session_id="session-1", user_id="user-1", task=task)
    await run.set_status("running")

    stream = _agent_run_event_generator(run)
    first = await anext(stream)
    snapshot = json.loads(first.removeprefix("data: ").strip())
    assert snapshot["type"] == "run_snapshot"

    await stream.aclose()
    assert not task.cancelled()
    assert not task.done()

    release.set()
    await task


async def test_reconnect_snapshot_preserves_tokens_and_latest_parts():
    run = AgentRun(id="run-1", session_id="session-1", user_id="user-1")
    await run.events.put({"type": "token", "delta": "Hel"})
    await run.events.put({"type": "token", "delta": "lo"})
    await run.events.put({"type": "part.updated", "part": {"id": "step-1", "status": "running"}})
    await run.events.put({"type": "part.updated", "part": {"id": "step-1", "status": "completed"}})

    snapshot = await run.events.snapshot(run)
    assert snapshot["streaming_content"] == "Hello"
    assert snapshot["parts"] == [{"id": "step-1", "status": "completed"}]


async def test_reconnect_snapshot_preserves_viz_and_filter_events():
    run = AgentRun(id="run-1", session_id="session-1", user_id="user-1")
    await run.events.put({"type": "viz", "viz_id": "viz-1", "action": "created"})
    await run.events.put({"type": "filter", "filter_id": "filter-1", "action": "created"})

    snapshot = await run.events.snapshot(run)

    assert snapshot["artifact_events"] == [
        {"type": "viz", "viz_id": "viz-1", "action": "created", "sequence": 1},
        {"type": "filter", "filter_id": "filter-1", "action": "created", "sequence": 2},
    ]


async def test_registry_reuses_client_request_id_and_isolates_users():
    registry = AgentRunRegistry()
    first = await registry.create("session-1", "user-1", "request-1")
    duplicate = await registry.create("session-1", "user-1", "request-1")

    assert duplicate is first
    assert registry.get(first.id, "user-1") is first
    assert registry.get(first.id, "user-2") is None


async def test_registry_admission_is_atomic_and_idempotent():
    registry = AgentRunRegistry()

    first, second = await asyncio.gather(
        registry.admit("session-1", "user-1", "request-1", max_active=3),
        registry.admit("session-1", "user-1", "request-1", max_active=3),
    )

    assert first[0] is second[0]
    assert sorted((first[1], second[1])) == [False, True]


async def test_registry_rejects_concurrent_distinct_requests_for_one_session():
    registry = AgentRunRegistry()
    await registry.admit("session-1", "user-1", "request-1", max_active=3)

    with pytest.raises(ActiveSessionRunError):
        await registry.admit("session-1", "user-1", "request-2", max_active=3)


async def test_executor_completes_and_stamps_run_id(monkeypatch):
    import agents.run_executor as run_executor
    from api.schemas import QueryRequest

    monkeypatch.setattr(run_executor, "over_limit_message_or_none", lambda: None)
    captured = {}

    class FakeOrchestrator:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return {
                "response": "Finished",
                "task_type": "analysis",
                "status": "completed",
                "metadata": {"parts": []},
            }

    session = SimpleNamespace()
    run = AgentRun(id="run-1", session_id="session-1", user_id="user-1")
    request = QueryRequest(session_id="session-1", message="Analyze")

    await execute_agent_run(
        run,
        request,
        orchestrator=FakeOrchestrator(),
        session=session,
        config={"server": {"stream_timeout_seconds": 5}},
        user_id="user-1",
        user_email="test@example.com",
        effective_model=None,
    )

    snapshot = await run.events.snapshot(run)
    assert run.status == "completed"
    assert captured["run_id"] == "run-1"
    assert snapshot["result"]["metadata"]["run_id"] == "run-1"


async def test_executor_terminalizes_base_exception_group(monkeypatch):
    import agents.run_executor as run_executor
    from api.schemas import QueryRequest

    monkeypatch.setattr(run_executor, "over_limit_message_or_none", lambda: None)

    class FakeOrchestrator:
        async def execute(self, **kwargs):
            raise BaseExceptionGroup("MCP failure", [BaseException("transport stopped")])

    messages = []
    session = SimpleNamespace(
        add_message=lambda role, content, metadata=None: messages.append(
            (role, content, metadata or {})
        )
    )
    run = AgentRun(id="run-1", session_id="session-1", user_id="user-1")

    await execute_agent_run(
        run,
        QueryRequest(session_id="session-1", message="Analyze"),
        orchestrator=FakeOrchestrator(),
        session=session,
        config={"server": {"stream_timeout_seconds": 5}},
        user_id="user-1",
        user_email="test@example.com",
        effective_model=None,
    )

    snapshot = await run.events.snapshot(run)
    assert run.status == "failed"
    assert snapshot["terminal_event"]["type"] == "error"
    assert messages[-1][2]["run_id"] == "run-1"


def test_run_endpoints_start_discover_and_explicitly_cancel(monkeypatch):
    import agents.run_executor as run_executor
    import api.agent_run_routes as run_routes

    registry = AgentRunRegistry()
    session = SimpleNamespace(metadata={}, messages=[])

    def add_message(role, content, metadata=None):
        session.messages.append(
            SimpleNamespace(role=role, content=content, metadata=metadata or {})
        )

    session.add_message = add_message

    class FakeStateManager:
        def get_session(self, session_id, app_user_id=None):
            return session if session_id == "session-1" and app_user_id == "user-1" else None

    class FakeOrchestrator:
        async def execute(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(run_routes, "agent_run_registry", registry)
    monkeypatch.setattr(run_routes, "state_manager", FakeStateManager())
    monkeypatch.setattr(run_routes, "resolve_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(
        run_routes,
        "get_usage_tracker",
        lambda: SimpleNamespace(is_within_quota=lambda user_id: (True, None)),
    )
    monkeypatch.setattr(run_executor, "over_limit_message_or_none", lambda: None)

    app = FastAPI()
    app.include_router(run_routes.router)
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    app.dependency_overrides[get_current_user_record] = lambda: SimpleNamespace(
        preferred_model=None,
        email="test@example.com",
    )
    app.dependency_overrides[get_config] = lambda: {"server": {"stream_timeout_seconds": 60}}
    app.dependency_overrides[get_orchestrator] = lambda: FakeOrchestrator()

    with TestClient(app) as client:
        created = client.post(
            "/api/query/runs",
            json={
                "session_id": "session-1",
                "message": "Keep working",
                "client_request_id": "request-1",
            },
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]
        run = registry.get(run_id, "user-1")
        assert run is not None

        deadline = time.monotonic() + 1
        while run.status == "queued" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert run.status == "running"

        assert run.task is not None
        assert not run.task.done()
        discovered = client.get("/api/query/runs/session/session-1")
        assert discovered.status_code == 200
        assert discovered.json()["run"]["run_id"] == run_id

        cancelled = client.post(f"/api/query/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        deadline = time.monotonic() + 1
        while run.status != "cancelled" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert run.status == "cancelled"

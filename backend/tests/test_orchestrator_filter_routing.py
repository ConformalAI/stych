"""task_type="filter" short-circuits OrchestratorAgent.execute straight to the
filter dispatch — the ReAct planning loop must not run."""

from __future__ import annotations

import types
from datetime import datetime

import pandas as pd

from agents.orchestrator import agent as orch_mod
from agents.orchestrator.agent import OrchestratorAgent
from agents.state import AgentState, DataContext


def _state():
    raw = pd.DataFrame({"id": [1, 2], "x": [1.0, 2.0]})
    st = AgentState(session_id="sess-route")
    st.metadata["user_id"] = "u1"
    st.set_data_context(
        DataContext(
            file_id="f1",
            filename="d.csv",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    return st


async def _async_noop(*_args, **_kwargs):
    return None


def _orch(monkeypatch):
    orch = OrchestratorAgent(types.SimpleNamespace(), config={})
    # No-op memory hooks: retain_turn is scheduled via asyncio.create_task.
    orch.memory = types.SimpleNamespace(
        hindsight=None,
        get_context_window=lambda *a, **k: [],
        retain_turn=_async_noop,
    )
    return orch


async def test_filter_task_type_bypasses_react(monkeypatch):
    orch = _orch(monkeypatch)
    dispatched = []

    async def fake_dispatch(agent_id, sub, msg, state, sid, emitter, ua, domain_label=""):
        dispatched.append((agent_id, sub))
        return {
            "status": "completed",
            "response": "Saved the filter.",
            "metadata": {"filters": [{"filter_id": "abc"}]},
            "agents_used": ["filter"],
        }

    async def boom(*args, **kwargs):
        raise AssertionError("run_orchestrator_react must not run for task_type=filter")

    monkeypatch.setattr(orch, "_call_agent_dispatch", fake_dispatch)
    monkeypatch.setattr(orch_mod, "run_orchestrator_react", boom)

    result = await orch.execute("filter", "flag negative balances", _state())

    assert dispatched == [("filter", "flag negative balances")]
    assert result["response"] == "Saved the filter."
    assert result["task_type"] == "filter"
    assert result["agents_used"] == ["filter"]
    assert result["metadata"]["filters"] == [{"filter_id": "abc"}]


async def test_other_task_types_still_use_react(monkeypatch):
    orch = _orch(monkeypatch)
    ran = []

    async def fake_react(orch_arg, **kwargs):
        ran.append(kwargs["task_type"])
        return ("hi", "completed", "auto", [], {}, None)

    monkeypatch.setattr(orch_mod, "run_orchestrator_react", fake_react)

    result = await orch.execute("auto", "hello", _state())
    assert ran == ["auto"]
    assert result["response"] == "hi"


async def test_filter_dispatch_reaches_subagent(monkeypatch):
    """The 'filter' branch of _call_agent_dispatch routes to subagent_filter."""
    orch = _orch(monkeypatch)
    seen = []

    async def fake_execute(state, message, session_id=None, progress_emitter=None):
        seen.append(message)
        return {"status": "completed", "response": "ok", "metadata": {}}

    monkeypatch.setattr(orch.filter_agent, "execute", fake_execute)
    result = await orch._call_agent_dispatch(
        "filter", "make a filter", "make a filter", _state(), "s1", None, None
    )
    assert seen == ["make a filter"]
    assert result["agents_used"] == ["filter"]

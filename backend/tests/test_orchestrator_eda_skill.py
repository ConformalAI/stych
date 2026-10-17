from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from agents.orchestrator.agent import OrchestratorAgent
from agents.orchestrator.eda_skill import build_eda_skill_prompt
from agents.state import AgentState, DataContext


def _context(file_id: str, filename: str) -> DataContext:
    frame = pd.DataFrame({"value": [1, 2, 3]})
    return DataContext(
        file_id=file_id,
        filename=filename,
        raw_dataframe=frame.copy(deep=True),
        working_dataframe=frame.copy(deep=True),
        uploaded_at=datetime.now(),
    )


def _orchestrator() -> OrchestratorAgent:
    orch = object.__new__(OrchestratorAgent)
    orch.memory = SimpleNamespace(hindsight=None)
    orch._log = lambda *args, **kwargs: None
    return orch


def test_eda_skill_is_objective_based_not_a_fixed_pipeline():
    state = AgentState(session_id="session-1", data_context=_context("primary", "primary.csv"))

    prompt = build_eda_skill_prompt(state)

    assert "adapt" in prompt.lower()
    assert "use your judgment" in prompt.lower()
    assert "do not skip" not in prompt.lower()
    assert "follow these steps" not in prompt.lower()


async def test_eda_scopes_shared_engine_to_requested_context_and_restores_primary(monkeypatch):
    import agents.orchestrator.agent as agent_module

    primary = _context("primary", "primary.csv")
    secondary = _context("secondary", "secondary.csv")
    state = AgentState(
        session_id="session-1",
        data_context=primary,
        additional_data_contexts=[secondary],
        metadata={"long_term_memory_enabled": False},
    )
    captured = {}

    async def fake_react(orch, **kwargs):
        captured["active"] = kwargs["state"].data_context
        captured["contexts"] = list(kwargs["state"].get_all_data_contexts())
        return "EDA complete", "completed", "auto", ["analysis"], {}, None

    monkeypatch.setattr(agent_module, "run_orchestrator_react", fake_react)

    result = await _orchestrator().execute(
        task_type="eda",
        message="Explore the selected dataset",
        state=state,
        target_file_id="secondary",
    )

    assert result["status"] == "completed"
    assert captured["active"] is secondary
    assert captured["contexts"] == [secondary, primary]
    assert state.data_context is primary
    assert state.additional_data_contexts == [secondary]


async def test_eda_missing_target_returns_failed_result(monkeypatch):
    import agents.orchestrator.agent as agent_module

    state = AgentState(
        session_id="session-1",
        data_context=_context("primary", "primary.csv"),
        metadata={"long_term_memory_enabled": False},
    )
    called = False

    async def fake_react(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("the engine must not run for a missing target")

    monkeypatch.setattr(agent_module, "run_orchestrator_react", fake_react)

    result = await _orchestrator().execute(
        task_type="eda",
        message="Explore the selected dataset",
        state=state,
        target_file_id="missing",
        run_id="run-1",
    )

    assert called is False
    assert result == {"error": "Dataset not found in session.", "success": False}
    assert state.messages[-1].metadata == {"status": "error", "run_id": "run-1"}

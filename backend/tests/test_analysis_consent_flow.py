from datetime import datetime

import pandas as pd
import pytest

from agents.analysis.agent import (
    ACTION_KIND_CLEANING_CONSENT,
    PENDING_KEY,
    PENDING_TYPE_TOOL_NEEDS_CLEANING,
    STATUS_COMPLETED,
    DataAnalysisAgent,
)
from agents.state import AgentState, DataContext
from tools.analysis_tools import ReactToolRegistry
from tools.workspace_toolset import WorkspaceToolset


class _DummyRouter:
    async def generate(self, request):
        raise RuntimeError("LLM call should be mocked in this test")


@pytest.mark.asyncio
async def test_needs_cleaning_tool_sets_halt_for_permission(monkeypatch):
    """The needs_cleaning tool records quality and requests a user permission HITL."""
    agent = DataAnalysisAgent(_DummyRouter())
    raw_df = pd.DataFrame({"Warehouse": ["Warehouse 2"], "Price": [20.0]})
    state = AgentState(session_id="session-1")
    state.set_data_context(
        DataContext(
            file_id="f1",
            filename="sample.csv",
            raw_dataframe=raw_df.copy(deep=True),
            working_dataframe=raw_df.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    reg = ReactToolRegistry(
        WorkspaceToolset(),
        None,
        workspace_root=None,
        quality_summary_fn=agent._build_quality_summary,
    )
    out = await reg.call("needs_cleaning", {"reason": "missing values"}, raw_df, state)
    assert "Request recorded" in out
    halt = state.metadata.get("analysis_react_halt")
    assert isinstance(halt, dict)
    assert halt.get("kind") == "needs_cleaning_permission"
    assert halt.get("reason") == "missing values"
    state.metadata.pop("analysis_react_halt", None)


@pytest.mark.asyncio
async def test_tool_needs_cleaning_consent_accepts_chains_react_resume(monkeypatch):
    """
    Resuming after consent runs cleaning then resumes the paused unified engine
    transcript (cleaning + the engine resume are mocked).
    """
    agent = DataAnalysisAgent(_DummyRouter())
    raw_df = pd.DataFrame({"Warehouse": ["Warehouse 2"], "Price": [20.0]})
    state = AgentState(session_id="session-1")
    state.set_data_context(
        DataContext(
            file_id="f1",
            filename="sample.csv",
            raw_dataframe=raw_df.copy(deep=True),
            working_dataframe=raw_df.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    # Every needs-cleaning pause is stored with engine=True + the paused transcript.
    state.metadata[PENDING_KEY] = {
        "type": PENDING_TYPE_TOOL_NEEDS_CLEANING,
        "engine": True,
        "engine_pending": {"transcript": [], "pending": {}},
        "prompt_id": "pid-1",
        "original_question": "Average price?",
        "halt_reason": "gaps",
        "quality_summary": {
            "missing_ratio": 0.1,
            "duplicate_ratio": 0.0,
            "dtype_mismatch_columns": [],
            "outlier_columns": [],
        },
    }

    async def _fake_process_cleaning(state, message, user_action=None, progress_emitter=None):
        return {
            "status": STATUS_COMPLETED,
            "response": "OK",
            "summary": "cleaned",
            "metadata": {},
        }

    monkeypatch.setattr(agent.cleaning_agent, "process_query", _fake_process_cleaning)

    observed: dict = {}

    async def _fake_resume(state, engine_pending, obs, original_question, progress_emitter=None):
        observed["resumed"] = True
        observed["question"] = original_question
        return {"status": STATUS_COMPLETED, "response": "done", "metadata": {}}

    monkeypatch.setattr(agent, "_resume_unified_loop", _fake_resume)

    res = await agent.process_query(
        state=state,
        message="",
        user_action={
            "prompt_id": "pid-1",
            "kind": ACTION_KIND_CLEANING_CONSENT,
            "value": "accept",
        },
    )
    assert res["status"] == STATUS_COMPLETED
    assert observed.get("resumed") is True
    assert observed.get("question") == "Average price?"
    assert PENDING_KEY not in state.metadata


@pytest.mark.asyncio
async def test_readiness_not_auto_run_on_first_message(monkeypatch):
    """
    The old pre-ReAct automatic readiness / consent block is not used; analysis
    goes straight to the ReAct entry path (mocked here).
    """
    agent = DataAnalysisAgent(_DummyRouter())
    raw_df = pd.DataFrame({"a": [1]})
    state = AgentState(session_id="session-2")
    state.set_data_context(
        DataContext(
            file_id="f1",
            filename="sample.csv",
            raw_dataframe=raw_df.copy(deep=True),
            working_dataframe=raw_df.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    ars = {"c": 0}

    async def _fake_assess(*args, **kwargs):
        ars["c"] += 1
        return {"status": "READY", "quality_summary": {}}

    monkeypatch.setattr(agent, "assess_readiness", _fake_assess)

    async def _fake_exec(*a, **k):
        return {"status": STATUS_COMPLETED, "response": "ok", "metadata": {}}

    monkeypatch.setattr(agent, "_execute_analysis", _fake_exec)
    r = await agent.process_query(state, "what is the mean?")
    assert r["status"] == STATUS_COMPLETED
    assert ars["c"] == 0

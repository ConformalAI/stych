"""
Unified-engine path for the cleaning agent (agents.use_unified_engine = true).

Covers the two HITL pauses — question and permission — and their resume,
confirming the user's answer / permission decision becomes the paused tool's
native result on the resumed transcript.
"""

from datetime import datetime

import pandas as pd
import pytest

from agents.cleaning.agent import (
    CLEANING_ENGINE_PENDING_KEY,
    STATUS_AWAITING_USER_ACTION,
    STATUS_COMPLETED,
    DataCleaningAgent,
)
from agents.state import AgentState, DataContext
from llm.models import LLMResponse

_CFG = {"agents": {"use_unified_engine": True}, "cleaning": {"react_max_steps": 6}}


class _ScriptedRouter:
    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    async def generate(self, request):
        self.requests.append([m.model_dump(exclude_none=True) for m in request.messages])
        return self._script.pop(0)


def _state():
    raw = pd.DataFrame({"a": [1, 2, 2], "b": ["x", None, "y"]})
    st = AgentState(session_id="sess-clean")
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


def _tool(name, args, call_id="c1"):
    return LLMResponse(
        content="",
        model="fake",
        metadata={"tool_calls": [{"id": call_id, "name": name, "arguments": args}]},
    )


def _final(text):
    return LLMResponse(content=text, model="fake", metadata={})


def _agent(script, monkeypatch):
    agent = DataCleaningAgent(_ScriptedRouter(script), config=_CFG)
    # Isolate the engine from the real workspace/filesystem.
    monkeypatch.setattr(agent, "_prepare_workspace", lambda state: "Workspace: /tmp/test")
    return agent


def _make_sql(state):
    state.data_context.metadata.update(
        {"source": "postgres", "connection_url": "postgresql://fake/db"}
    )
    return state


@pytest.mark.asyncio
async def test_unified_question_pause_resume(monkeypatch):
    script = [
        _tool(
            "question",
            {"question": "Drop nulls or impute?", "options": [{"label": "Drop", "value": "drop"}]},
            call_id="q1",
        ),
        _final("Done — dropped null rows."),
    ]
    agent = _agent(script, monkeypatch)
    state = _state()

    paused = await agent.process_query(state=state, message="clean it")
    assert paused["status"] == STATUS_AWAITING_USER_ACTION
    assert paused["action_prompt"]["kind"] == "cleaning_react_question"
    pend = state.metadata[CLEANING_ENGINE_PENDING_KEY]
    assert pend["engine_pending"]["pending"]["paused_call_id"] == "q1"

    resumed = await agent.process_query(
        state=state,
        message="",
        user_action={
            "kind": "cleaning_react_question",
            "prompt_id": pend["prompt_id"],
            "value": "drop",
        },
    )
    assert resumed["status"] == STATUS_COMPLETED
    assert "dropped" in resumed["response"].lower()
    assert CLEANING_ENGINE_PENDING_KEY not in state.metadata

    # The resumed transcript carries the answer as the question tool's result.
    resume_req = agent.llm_router.requests[-1]
    tool_msgs = [m for m in resume_req if m["role"] == "tool" and m.get("tool_call_id") == "q1"]
    assert tool_msgs and "drop" in tool_msgs[0]["content"].lower()


@pytest.mark.asyncio
async def test_unified_permission_deny_resume(monkeypatch):
    # create_view is a SQL-session tool: it must actually be offered (the
    # Toolkit rejects un-offered tools before the permission machinery), and
    # its args must pass schema validation before the user is ever prompted.
    script = [
        _tool("create_view", {"view_name": "v", "target": "temp"}, call_id="perm1"),
        _final("Understood — I did not create the view."),
    ]
    agent = _agent(script, monkeypatch)
    state = _make_sql(_state())

    paused = await agent.process_query(state=state, message="make a view")
    assert paused["status"] == STATUS_AWAITING_USER_ACTION
    assert paused["action_prompt"]["kind"] == "cleaning_react_permission"
    pend = state.metadata[CLEANING_ENGINE_PENDING_KEY]
    assert pend["tool"] == "create_view"

    resumed = await agent.process_query(
        state=state,
        message="",
        user_action={
            "kind": "cleaning_react_permission",
            "prompt_id": pend["prompt_id"],
            "value": "decline",
        },
    )
    assert resumed["status"] == STATUS_COMPLETED
    # The denied permission becomes the tool's native result.
    resume_req = agent.llm_router.requests[-1]
    tool_msgs = [m for m in resume_req if m["role"] == "tool" and m.get("tool_call_id") == "perm1"]
    assert tool_msgs and "denied" in tool_msgs[0]["content"].lower()


def test_private_working_copy_is_not_database_writeback_intent():
    message = (
        "Create a private named working copy called databricks_space_columns. "
        "Update that working copy only and do not modify the source table."
    )
    assert DataCleaningAgent._writeback_requested(message) is False
    assert (
        DataCleaningAgent._writeback_requested(
            "Persist the cleaned rows into a permanent database copy table."
        )
        is True
    )


@pytest.mark.asyncio
async def test_private_working_copy_refuses_materialize_without_prompting(monkeypatch):
    agent = _agent(
        [
            _tool("sql_materialize_copy", {"approved": True}, call_id="persist1"),
            _final("Kept the result in the private working copy."),
        ],
        monkeypatch,
    )
    state = _make_sql(_state())

    out = await agent.process_query(
        state=state,
        message=(
            "Create a private named working copy, change one row there, and do not persist "
            "anything outside Stych scratch."
        ),
    )

    assert out["status"] == STATUS_COMPLETED
    assert out.get("action_prompt") is None
    tool_msgs = [m for m in agent.llm_router.requests[-1] if m["role"] == "tool"]
    assert tool_msgs and "SCOPE_ERROR" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_failed_approved_persistent_write_does_not_prompt_twice(monkeypatch):
    agent = _agent(
        [
            _tool("sql_materialize_copy", {"approved": True}, call_id="persist1"),
            _final("The permanent copy could not be created."),
        ],
        monkeypatch,
    )
    state = _make_sql(_state())

    paused = await agent.process_query(
        state=state,
        message="Persist the cleaned rows into a permanent database copy table.",
    )
    assert paused["status"] == STATUS_AWAITING_USER_ACTION
    pending = state.metadata[CLEANING_ENGINE_PENDING_KEY]

    async def fail_materialize(self, tool_name, tool_input, state=None):
        return "SQL ERROR: simulated write failure"

    monkeypatch.setattr(
        "agents.cleaning.agent.CleaningReactToolRegistry.call",
        fail_materialize,
    )
    resumed = await agent.process_query(
        state=state,
        message="",
        user_action={
            "kind": "cleaning_react_permission",
            "prompt_id": pending["prompt_id"],
            "value": "accept",
        },
    )

    assert resumed["status"] == STATUS_COMPLETED
    assert resumed.get("action_prompt") is None
    assert CLEANING_ENGINE_PENDING_KEY not in state.metadata
    assert len(agent.llm_router.requests) == 2


@pytest.mark.asyncio
async def test_unified_mismatched_prompt_id_reprompts(monkeypatch):
    script = [_tool("question", {"question": "?", "options": []}, call_id="q1"), _final("x")]
    agent = _agent(script, monkeypatch)
    state = _state()
    await agent.process_query(state=state, message="clean")
    # Wrong prompt_id → re-prompt, pending preserved.
    out = await agent.process_query(
        state=state,
        message="",
        user_action={"kind": "cleaning_react_question", "prompt_id": "WRONG", "value": "x"},
    )
    assert out["status"] == STATUS_AWAITING_USER_ACTION
    assert CLEANING_ENGINE_PENDING_KEY in state.metadata

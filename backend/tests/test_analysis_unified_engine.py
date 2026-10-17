"""
Unified-engine path for the analysis agent (agents.use_unified_engine = true).

Covers a fresh run that finishes, and the needs_cleaning HITL that pauses,
hands off to the cleaning agent, and resumes the persisted transcript.
"""

from datetime import datetime

import pandas as pd
import pytest

from agents.analysis.agent import (
    PENDING_KEY,
    STATUS_AWAITING_USER_ACTION,
    STATUS_COMPLETED,
    DataAnalysisAgent,
)
from agents.state import AgentState, DataContext
from llm.models import LLMResponse

_UNIFIED_CONFIG = {"agents": {"use_unified_engine": True}, "analysis": {"react_max_steps": 4}}


class _ScriptedRouter:
    """Pops scripted LLMResponses; records the transcript of each request."""

    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    async def generate(self, request):
        self.requests.append([m.model_dump(exclude_none=True) for m in request.messages])
        return self._script.pop(0)


def _state():
    raw = pd.DataFrame({"Warehouse": ["W2", "W2"], "Price": [20.0, 30.0]})
    st = AgentState(session_id="sess-unified")
    st.set_data_context(
        DataContext(
            file_id="f1",
            filename="sample.csv",
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


@pytest.mark.asyncio
async def test_unified_fresh_run_completes_with_native_transcript():
    router = _ScriptedRouter([_tool("get_schema", {}), _final("Two columns: Warehouse, Price.")])
    agent = DataAnalysisAgent(router, config=_UNIFIED_CONFIG)
    state = _state()

    out = await agent.process_query(state=state, message="Describe the schema")
    assert out["status"] == STATUS_COMPLETED
    assert "Warehouse" in out["response"]

    # Second request carries a native transcript: assistant(tool_calls) + tool(result).
    second = router.requests[1]
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in second)
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_unified_needs_cleaning_pause_then_resume(monkeypatch):
    # Turn 1: model asks to clean → engine pauses.
    router = _ScriptedRouter(
        [
            _tool("needs_cleaning", {"reason": "missing values"}, call_id="p1"),
            # Resume turn: model produces the final answer.
            _final("Average price is 25.0 after cleaning."),
        ]
    )
    agent = DataAnalysisAgent(router, config=_UNIFIED_CONFIG)
    state = _state()

    paused = await agent.process_query(state=state, message="What is the average price?")
    assert paused["status"] == STATUS_AWAITING_USER_ACTION
    assert paused["action_prompt"]["kind"] == "cleaning_consent"
    pend = state.metadata.get(PENDING_KEY)
    assert pend and pend.get("engine") is True
    assert pend["engine_pending"]["pending"]["paused_call_id"] == "p1"

    # The cleaning sub-agent is mocked to complete immediately.
    async def _fake_cleaning(state, message, user_action=None, progress_emitter=None):
        return {
            "status": STATUS_COMPLETED,
            "response": "cleaned",
            "summary": "cleaned",
            "metadata": {},
        }

    monkeypatch.setattr(agent.cleaning_agent, "process_query", _fake_cleaning)

    resumed = await agent.process_query(
        state=state,
        message="",
        user_action={"prompt_id": pend["prompt_id"], "kind": "cleaning_consent", "value": "accept"},
    )
    assert resumed["status"] == STATUS_COMPLETED
    assert "Average price" in resumed["response"]
    assert PENDING_KEY not in state.metadata

    # The resume request must contain the paused tool's result carrying the
    # cleaning outcome (native protocol — not a fabricated user message).
    resume_req = router.requests[-1]
    tool_msgs = [m for m in resume_req if m["role"] == "tool" and m.get("tool_call_id") == "p1"]
    assert tool_msgs and "Cleaning completed" in tool_msgs[0]["content"]

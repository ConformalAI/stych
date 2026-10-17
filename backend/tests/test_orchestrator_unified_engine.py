"""
Unified-engine path for the orchestrator (agents.use_unified_engine = true).

Covers a fresh multi-agent run and the sub-agent HITL case: a called sub-agent
returns awaiting_user_action → the orchestrator pauses → on the next turn it
re-invokes the sub-agent with the user's answer and resumes the transcript.

Driven through a lightweight fake orchestrator so the full agent tree is not
constructed; the engine + pause/resume plumbing is what's under test.
"""

from __future__ import annotations

import types

import pytest

from agents.orchestrator.orchestrator_react import (
    ORCH_ENGINE_PENDING_KEY,
    run_orchestrator_unified,
)
from agents.state import AgentState
from llm.models import LLMResponse


class _ScriptedRouter:
    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    async def generate(self, request):
        self.requests.append([m.model_dump(exclude_none=True) for m in request.messages])
        return self._script.pop(0)


def _tool(name, args, call_id="c1"):
    return LLMResponse(
        content="",
        model="fake",
        metadata={"tool_calls": [{"id": call_id, "name": name, "arguments": args}]},
    )


def _final(text):
    return LLMResponse(content=text, model="fake", metadata={})


def _fake_orch(script, dispatch):
    orch = types.SimpleNamespace()
    orch.llm_router = _ScriptedRouter(script)
    orch._config = {"routing": {"orchestrate_react": {"react_max_steps": 8}}}
    orch.memory = None
    orch._router_model = lambda: None
    orch._call_agent_dispatch = dispatch
    return orch


def _state():
    st = AgentState(session_id="sess-orch")
    st.metadata["user_id"] = "u1"
    return st


@pytest.mark.asyncio
async def test_unified_fresh_call_agent_then_final():
    calls = []

    async def dispatch(agent, sub, msg, state, sid, emitter, ua, domain_label=""):
        calls.append(agent)
        return {
            "status": "completed",
            "response": "QA: 42 rows.",
            "agents_used": [agent],
            "metadata": {},
        }

    orch = _fake_orch(
        [_tool("call_agent", {"agent": "qa", "prompt": "row count?"}), _final("You have 42 rows.")],
        dispatch,
    )
    state = _state()

    resp, status, label, agents, meta, ap = await run_orchestrator_unified(
        orch,
        task_type="auto",
        message="how many rows?",
        state=state,
        session_id="sess-orch",
        progress_emitter=None,
    )
    assert status == "completed"
    assert resp == "You have 42 rows."
    assert agents == ["qa"]
    assert calls == ["qa"]

    # Native transcript: the call_agent result came back as a role:"tool" message.
    second = orch.llm_router.requests[1]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert tool_msgs and "[status: completed]" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_unified_call_agent_threads_named_source(monkeypatch):
    """The orchestrator's call_agent `input` (a named source) must reach the
    sub-agent: promote it to active via _activate_named_source AND bake it into
    the sub-task prompt. Regression guard for the unified path dropping `input`."""
    import agents.orchestrator.orchestrator_react as mod

    activated = []
    monkeypatch.setattr(
        mod, "_activate_named_source", lambda state, sid, src: activated.append(src)
    )

    seen = {}

    async def dispatch(agent, sub, msg, state, sid, emitter, ua, domain_label=""):
        seen["agent"] = agent
        seen["sub"] = sub
        return {"status": "completed", "response": "done", "agents_used": [agent], "metadata": {}}

    orch = _fake_orch(
        [
            _tool(
                "call_agent",
                {
                    "agent": "cleaning",
                    "input": "Cafe_Sales_Data_filled.parquet",
                    "prompt": "Fill missing Payment Method with the mode.",
                },
            ),
            _final("Filled the column."),
        ],
        dispatch,
    )
    state = _state()

    resp, status, _, agents, meta, ap = await run_orchestrator_unified(
        orch,
        task_type="auto",
        message="fill payment method",
        state=state,
        session_id="sess-orch",
        progress_emitter=None,
    )
    assert status == "completed"
    # The named source was promoted to active…
    assert activated == ["Cafe_Sales_Data_filled.parquet"]
    # …and baked into the prompt the sub-agent received.
    assert "Cafe_Sales_Data_filled.parquet" in seen["sub"]
    assert "Fill missing Payment Method" in seen["sub"]


@pytest.mark.asyncio
async def test_unified_subagent_hitl_pause_then_resume():
    state = _state()
    phase = {"resumed": False}

    async def dispatch(agent, sub, msg, state, sid, emitter, ua, domain_label=""):
        if not phase["resumed"]:
            # First call → sub-agent needs user input.
            return {
                "status": "awaiting_user_action",
                "response": "Clean first?",
                "action_prompt": {"prompt_id": "ap1", "kind": "cleaning_consent"},
                "agents_used": [agent],
                "metadata": {},
            }
        return {
            "status": "completed",
            "response": "Cleaned and counted: 40.",
            "agents_used": [agent],
            "metadata": {},
        }

    orch = _fake_orch(
        [
            _tool("call_agent", {"agent": "analysis", "prompt": "avg price"}, call_id="ca1"),
            _final("The average price is 9.99."),
        ],
        dispatch,
    )

    paused = await run_orchestrator_unified(
        orch,
        task_type="auto",
        message="average price?",
        state=state,
        session_id="sess-orch",
        progress_emitter=None,
    )
    resp, status, _, agents, meta, ap = paused
    assert status == "awaiting_user_action"
    assert ap["prompt_id"] == "ap1"
    pend = state.metadata[ORCH_ENGINE_PENDING_KEY]
    assert pend["in_flight"]["agent"] == "analysis"
    assert pend["engine_pending"]["pending"]["paused_call_id"] == "ca1"

    # Resume: user answers; sub-agent now completes; engine continues to final.
    phase["resumed"] = True
    resumed = await run_orchestrator_unified(
        orch,
        task_type="auto",
        message="__user_action__",
        state=state,
        session_id="sess-orch",
        progress_emitter=None,
        user_action={"prompt_id": "ap1", "kind": "cleaning_consent", "value": "accept"},
    )
    resp2, status2, _, agents2, meta2, ap2 = resumed
    assert status2 == "completed"
    assert resp2 == "The average price is 9.99."
    assert ORCH_ENGINE_PENDING_KEY not in state.metadata

    # The resume request carries the sub-agent's completion as the paused
    # call_agent's native tool result.
    last = orch.llm_router.requests[-1]
    tool_msgs = [m for m in last if m["role"] == "tool" and m.get("tool_call_id") == "ca1"]
    assert tool_msgs and "Cleaned and counted" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_confirmed_linking_is_terminal_and_cannot_request_confirmation_twice():
    state = _state()
    calls = []

    async def dispatch(agent, sub, msg, state, sid, emitter, ua, domain_label=""):
        calls.append((agent, ua))
        if ua is None:
            return {
                "status": "awaiting_user_action",
                "response": "Execute this join?",
                "action_prompt": {"prompt_id": "join-1", "kind": "linking_confirm"},
                "agents_used": [agent],
                "metadata": {},
            }
        return {
            "status": "completed",
            "response": "Joined 7,773 rows.",
            "agents_used": [agent],
            "metadata": {"linking": {"rows": 7773}},
        }

    # Only the initial orchestration decision is scripted.  A second model
    # request after confirmation would exhaust the script and fail this test.
    orch = _fake_orch(
        [_tool("call_agent", {"agent": "linking", "prompt": "join both tables"})],
        dispatch,
    )

    paused = await run_orchestrator_unified(
        orch,
        task_type="auto",
        message="join both tables",
        state=state,
        session_id="sess-orch",
        progress_emitter=None,
    )
    assert paused[1] == "awaiting_user_action"

    resumed = await run_orchestrator_unified(
        orch,
        task_type="auto",
        message="__user_action__",
        state=state,
        session_id="sess-orch",
        progress_emitter=None,
        user_action={"prompt_id": "join-1", "kind": "linking_confirm", "value": "accept"},
    )

    response, status, _, agents, metadata, action_prompt = resumed
    assert response == "Joined 7,773 rows."
    assert status == "completed"
    assert agents == ["linking"]
    assert metadata["linking"] == {"rows": 7773}
    assert action_prompt is None
    assert ORCH_ENGINE_PENDING_KEY not in state.metadata
    assert len(orch.llm_router.requests) == 1
    assert [agent for agent, _ in calls] == ["linking", "linking"]

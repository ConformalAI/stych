"""
Tests for the unified transcript-driven engine (agents/react/loop.py).

Modules are loaded in isolation (mirroring tests/test_react_engine.py) so the
heavy agents/__init__ and llm/__init__ are never imported. A scripted fake LLM
router drives the loop without any network calls.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from llm import models
from react_engine import compaction, loop

Message = models.Message


def test_compaction_shrinks_content_but_preserves_structure():
    big = "x" * 5000
    transcript = [Message(role="system", content="sys"), Message(role="user", content="task")]
    # 8 step-pairs of assistant(tool_call) + large tool result.
    for i in range(8):
        transcript.append(
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            )
        )
        transcript.append(Message(role="tool", tool_call_id=f"c{i}", content=big))

    out, did = compaction.maybe_compact(transcript, "claude-sonnet-4", budget_fraction=0.001)
    assert did is True
    # Structural invariant: same number of messages, roles unchanged, ids intact.
    assert len(out) == len(transcript)
    assert [m.role for m in out] == [m.role for m in transcript]
    for orig, new in zip(transcript, out):
        if orig.tool_calls:
            assert new.tool_calls == orig.tool_calls  # tool-call framing never touched
    # Old tool results were shrunk; system + task + recent window preserved verbatim.
    assert out[0].content == "sys"
    assert out[1].content == "task"
    assert len(out[3].content) < len(big)  # an early tool result shrank
    assert out[-1].content == big  # the most-recent tool result kept verbatim


class FakeResponse:
    def __init__(self, content="", tool_calls=None, finish_reason=None):
        self.content = content
        self.metadata = {"tool_calls": tool_calls}
        self.finish_reason = finish_reason


class FakeRouter:
    """Returns scripted responses; records the messages it was asked to generate."""

    def __init__(self, script: List[FakeResponse]):
        self._script = list(script)
        self.seen_requests: List[List[Dict[str, Any]]] = []

    async def generate(self, request):
        self.seen_requests.append([m.model_dump(exclude_none=True) for m in request.messages])
        return self._script.pop(0)


def _seed(task="do the thing"):
    return [Message(role="system", content="sys"), Message(role="user", content=task)]


@pytest.mark.asyncio
async def test_single_tool_then_final():
    router = FakeRouter(
        [
            FakeResponse(
                content="thinking", tool_calls=[{"id": "c1", "name": "ping", "arguments": {"x": 1}}]
            ),
            FakeResponse(content="all done", tool_calls=None),
        ]
    )

    calls = []

    async def executor(name, args, call_id):
        calls.append((name, args, call_id))
        return "pong"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{"t": 1}],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=5,
    )
    assert out["status"] == "completed"
    assert out["response"] == "all done"
    assert calls == [("ping", {"x": 1}, "c1")]

    # The SECOND request must show a valid native transcript:
    # system, user, assistant(tool_calls), tool(result).
    second = router.seen_requests[1]
    assert second[2]["role"] == "assistant"
    assert second[2]["tool_calls"][0]["id"] == "c1"
    assert second[3]["role"] == "tool"
    assert second[3]["tool_call_id"] == "c1"
    assert second[3]["content"] == "pong"


@pytest.mark.asyncio
async def test_synthesizes_id_when_provider_omits_it():
    router = FakeRouter(
        [
            FakeResponse(tool_calls=[{"id": None, "name": "ping", "arguments": {}}]),
            FakeResponse(content="done"),
        ]
    )

    seen_call_id = {}

    async def executor(name, args, call_id):
        seen_call_id["id"] = call_id
        return "ok"

    out = await loop.run_agent_loop(
        llm_router=router, tools=[{}], transcript=_seed(), tool_executor=executor, max_steps=5
    )
    assert out["status"] == "completed"
    # A non-null id was synthesized and used for both the assistant call and result.
    assert seen_call_id["id"]
    second = router.seen_requests[1]
    assert second[2]["tool_calls"][0]["id"] == seen_call_id["id"]
    assert second[3]["tool_call_id"] == seen_call_id["id"]


@pytest.mark.asyncio
async def test_pause_and_resume_roundtrip():
    # Turn 1: model calls a tool that pauses for user input.
    router1 = FakeRouter(
        [
            FakeResponse(
                content="need consent",
                tool_calls=[
                    {"id": "p1", "name": "needs_cleaning", "arguments": {"reason": "nulls"}}
                ],
            )
        ]
    )

    async def executor_pause(name, args, call_id):
        raise loop.PauseSignal(
            {"question": "Clean first?", "prompt_id": "x"}, response="May I clean?"
        )

    paused = await loop.run_agent_loop(
        llm_router=router1,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor_pause,
        max_steps=5,
    )
    assert paused["status"] == "awaiting_user_action"
    assert paused["action_prompt"]["question"] == "Clean first?"
    pending = paused["metadata"]["pending"]
    # The persisted transcript must NOT yet contain the in-flight assistant turn.
    assert all(m["role"] in ("system", "user") for m in paused["metadata"]["transcript"])

    # Turn 2: resume with the user's decision as the paused tool's result.
    router2 = FakeRouter([FakeResponse(content="cleaned and analyzed")])

    async def executor_after(name, args, call_id):
        raise AssertionError("no tool should run on this resume turn")

    resumed = await loop.run_agent_loop(
        llm_router=router2,
        tools=[{}],
        transcript=[],
        tool_executor=executor_after,
        max_steps=5,
        resume={"transcript": paused["metadata"]["transcript"], "pending": pending},
        resume_observation="User approved; cleaning complete.",
    )
    assert resumed["status"] == "completed"
    assert resumed["response"] == "cleaned and analyzed"

    # The resumed request must include the assistant call + the paused tool result
    # carrying the user's decision.
    req = router2.seen_requests[0]
    assistant = [m for m in req if m["role"] == "assistant" and m.get("tool_calls")]
    tool_msgs = [m for m in req if m["role"] == "tool"]
    assert assistant and assistant[0]["tool_calls"][0]["id"] == "p1"
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "p1"
    assert "User approved" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_budget_exhaustion_calls_summarizer():
    # Always returns a tool call → never terminates on its own.
    router = FakeRouter(
        [
            FakeResponse(tool_calls=[{"id": f"c{i}", "name": "noop", "arguments": {}}])
            for i in range(10)
        ]
    )

    async def executor(name, args, call_id):
        return "ok"

    async def summarizer(transcript):
        return "summary of partial work"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=3,
        budget_summarizer=summarizer,
    )
    assert out["status"] == "partial"
    assert out["response"] == "summary of partial work"
    assert out["metadata"]["budget_exhausted"] is True


_WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "parameters": {"type": "object", "required": ["path", "content"]},
    },
}


@pytest.mark.asyncio
async def test_truncated_tool_args_return_recoverable_observation():
    # Step 1: a write_file call whose args were coerced to {} because the output
    # was truncated (finish_reason="length"). Step 2: the model recovers.
    router = FakeRouter(
        [
            FakeResponse(
                content="writing a big summary",
                tool_calls=[{"id": "c1", "name": "write_file", "arguments": {}}],
                finish_reason="length",
            ),
            FakeResponse(content="recovered"),
        ]
    )

    calls = []

    async def executor(name, args, call_id):
        calls.append((name, args, call_id))
        return "should not run"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[_WRITE_FILE_TOOL],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=5,
    )

    assert out["status"] == "completed"
    # The broken call must NOT have been executed (no dead-end "missing path").
    assert calls == []
    # The model's next turn must have received the recoverable truncation message.
    second = router.seen_requests[1]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assert "truncated" in tool_msgs[0]["content"].lower()
    assert "run_script" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_truncation_without_missing_args_still_executes():
    # finish_reason="length" but the (no-arg) tool call is complete → execute it.
    router = FakeRouter(
        [
            FakeResponse(
                tool_calls=[{"id": "c1", "name": "get_schema", "arguments": {}}],
                finish_reason="length",
            ),
            FakeResponse(content="done"),
        ]
    )
    calls = []

    async def executor(name, args, call_id):
        calls.append(name)
        return "ok"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{"type": "function", "function": {"name": "get_schema", "parameters": {}}}],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=5,
    )
    assert out["status"] == "completed"
    assert calls == ["get_schema"]


def test_get_max_output_tokens_clamps_to_cap_and_handles_unknown():
    from memory.context_window import (
        _DEFAULT_OUTPUT_LIMIT,
        DEFAULT_OUTPUT_CAP,
        get_max_output_tokens,
    )

    # Claude Sonnet 4.x output limit (64k) is clamped to the cap.
    assert get_max_output_tokens("openrouter/anthropic/claude-sonnet-4.5") == DEFAULT_OUTPUT_CAP
    # A low cap wins over the model limit.
    assert get_max_output_tokens("claude-sonnet-4.5", cap=2000) == 2000
    # Unknown model falls back to the default output limit (under the cap).
    assert get_max_output_tokens("some-unknown-model") == min(
        _DEFAULT_OUTPUT_LIMIT, DEFAULT_OUTPUT_CAP
    )


def test_output_cap_from_config():
    assert loop.output_cap_from_config(None) is None
    assert loop.output_cap_from_config({}) is None
    assert loop.output_cap_from_config({"llm": {"react_output_cap": 12000}}) == 12000
    assert loop.output_cap_from_config({"llm": {}}) is None


# ---------------------------------------------------------------------------
# Parallel-safe batch execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_safe_batch_runs_concurrently():
    import asyncio

    router = FakeRouter(
        [
            FakeResponse(
                tool_calls=[
                    {"id": "a", "name": "probe", "arguments": {"k": 1}},
                    {"id": "b", "name": "probe", "arguments": {"k": 2}},
                ]
            ),
            FakeResponse(content="done"),
        ]
    )
    in_flight = {"now": 0, "peak": 0}

    async def executor(name, args, call_id):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1
        return f"r{args['k']}"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        parallel_safe_tools={"probe"},
        max_steps=5,
    )
    assert out["status"] == "completed"
    assert in_flight["peak"] == 2  # both calls overlapped
    second = router.seen_requests[1]
    # Results correlate by id and keep call order in the transcript.
    assert second[3]["tool_call_id"] == "a" and second[3]["content"] == "r1"
    assert second[4]["tool_call_id"] == "b" and second[4]["content"] == "r2"


@pytest.mark.asyncio
async def test_mixed_batch_stays_sequential():
    router = FakeRouter(
        [
            FakeResponse(
                tool_calls=[
                    {"id": "a", "name": "probe", "arguments": {}},
                    {"id": "b", "name": "writer", "arguments": {}},
                ]
            ),
            FakeResponse(content="done"),
        ]
    )
    order = []

    async def executor(name, args, call_id):
        order.append(f"start:{name}")
        order.append(f"end:{name}")
        return "ok"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        parallel_safe_tools={"probe"},  # writer is not safe → whole batch sequential
        max_steps=5,
    )
    assert out["status"] == "completed"
    assert order == ["start:probe", "end:probe", "start:writer", "end:writer"]


@pytest.mark.asyncio
async def test_multi_call_step_emits_distinct_call_index():
    # Regression: a step with 2+ tool calls used to emit every ReactStepRecord
    # with the same step number, so trace_parts derived the same id for both
    # calls and the second silently overwrote the first in the live trace.
    router = FakeRouter(
        [
            FakeResponse(
                tool_calls=[
                    {"id": "a", "name": "probe", "arguments": {"k": 1}},
                    {"id": "b", "name": "probe", "arguments": {"k": 2}},
                ]
            ),
            FakeResponse(content="done"),
        ]
    )

    async def executor(name, args, call_id):
        return "ok"

    records = []

    async def emitter(rec):
        records.append((rec.phase, rec.action, rec.call_index))

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        parallel_safe_tools={"probe"},
        max_steps=5,
        emitter=emitter,
    )
    assert out["status"] == "completed"
    starts = [r for r in records if r[0] == "start"]
    ends = [r for r in records if r[0] == "end"]
    assert sorted(r[2] for r in starts) == [0, 1]
    assert sorted(r[2] for r in ends) == [0, 1]


class FakeStreamChunk:
    def __init__(self, content="", finish_reason=None, metadata=None):
        self.content = content
        self.finish_reason = finish_reason
        self.metadata = metadata or {}


class FakeStreamingRouter:
    """Streams a prose preamble before the tool-calls sentinel, like Claude does."""

    async def generate_stream(self, request):
        for word in ("Let ", "me ", "check. "):
            yield FakeStreamChunk(content=word, metadata={"is_tool_call": False})
        yield FakeStreamChunk(content="", metadata={"is_tool_call": True})
        yield FakeStreamChunk(
            content="",
            metadata={"tool_calls": [{"id": "c1", "name": "ping", "arguments": {}}]},
        )


@pytest.mark.asyncio
async def test_streamed_preamble_before_tool_call_triggers_retract():
    # Regression: stream_final streams tokens live before the loop knows the
    # turn will carry a tool call. Without a retraction signal, the UI's
    # "answer is streaming" flag latches true for the rest of the run and
    # never shows the between-steps trace loader again.
    streamed = []
    retracted = []

    async def emit_token(delta):
        streamed.append(delta)

    async def stream_retract():
        retracted.append(True)

    async def executor(name, args, call_id):
        return "pong"

    out = await loop.run_agent_loop(
        llm_router=FakeStreamingRouter(),
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=1,
        stream_final=True,
        emit_token=emit_token,
        stream_retract=stream_retract,
    )
    assert out["status"] == "partial"  # step budget hit after the one tool turn
    assert "".join(streamed) == "Let me check. "  # still streamed live
    assert len(retracted) == 1  # …but flagged for retraction, exactly once


# ---------------------------------------------------------------------------
# Repeat-failure guard
# ---------------------------------------------------------------------------


def _same_failing_call(step_id):
    return FakeResponse(tool_calls=[{"id": step_id, "name": "flaky", "arguments": {"q": "same"}}])


@pytest.mark.asyncio
async def test_repeat_failure_warns_then_aborts():
    responses = [_same_failing_call(f"c{i}") for i in range(6)]
    router = FakeRouter(responses)

    async def executor(name, args, call_id):
        return "ERROR: table does not exist"

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=20,
    )
    # Aborted at the 5th identical failure, well before max_steps.
    assert out["status"] == "partial"
    assert out["metadata"]["repeat_failure_abort"] == "flaky"
    assert out["metadata"]["steps_taken"] == 5
    # The 3rd/4th failures carried escalating guidance in the observation.
    warned = [
        m
        for req in router.seen_requests
        for m in req
        if m.get("role") == "tool" and "Do NOT repeat it" in (m.get("content") or "")
    ]
    assert warned


@pytest.mark.asyncio
async def test_repeat_guard_resets_on_success_and_ignores_different_args():
    responses = [
        _same_failing_call("c1"),
        FakeResponse(tool_calls=[{"id": "c2", "name": "flaky", "arguments": {"q": "other"}}]),
        _same_failing_call("c3"),
        FakeResponse(content="done"),
    ]
    router = FakeRouter(responses)
    outcomes = iter(["ERROR: boom", "fine", "ERROR: boom"])

    async def executor(name, args, call_id):
        return next(outcomes)

    out = await loop.run_agent_loop(
        llm_router=router,
        tools=[{}],
        transcript=_seed(),
        tool_executor=executor,
        max_steps=10,
    )
    # Different args are different fingerprints; two failures never warn or abort.
    assert out["status"] == "completed"

"""Trace parts: stable ids, collision-free nesting, emitter accumulation."""

import asyncio

from agents.progress import ProgressEmitter, _active_parent_part_id
from tracing.trace_parts import emit_react_part, orch_part_id, step_part


def test_orch_part_id_is_stable():
    assert orch_part_id(3) == "orch-3"


def test_child_part_id_derives_from_parent_and_step():
    p = step_part(agent="analysis", step=1, phase="start", parent_id="orch-2")
    assert p["id"] == "orch-2-1"
    assert p["parentId"] == "orch-2"
    assert p["type"] == "step"


def test_two_subagents_reusing_local_step_stay_distinct():
    # The core regression: Integration step 1 and Analysis step 1 under different
    # orchestrator parents must not collide (the old integer-step keying merged them).
    a = step_part(agent="integration", step=1, phase="end", parent_id="orch-1")
    b = step_part(agent="analysis", step=1, phase="end", parent_id="orch-3")
    assert a["id"] == "orch-1-1"
    assert b["id"] == "orch-3-1"
    assert a["id"] != b["id"]


def test_multi_call_step_ids_stay_distinct():
    # Regression: two tool calls issued in the same step (parallel-safe or not)
    # used to share f"{parent}-{step}" and silently overwrite each other.
    a = step_part(agent="analysis", step=1, phase="end", parent_id="orch-1", call_index=0)
    b = step_part(agent="analysis", step=1, phase="end", parent_id="orch-1", call_index=1)
    assert a["id"] == "orch-1-1"
    assert b["id"] == "orch-1-1-1"
    assert a["id"] != b["id"]


async def test_emit_part_upserts_by_id_and_streams_each_emission():
    queue: asyncio.Queue = asyncio.Queue()
    emitter = ProgressEmitter(queue)
    await emitter.emit_part(
        step_part(agent="orchestrator", step=1, phase="start", part_id="orch-1")
    )
    await emitter.emit_part(
        step_part(agent="orchestrator", step=1, phase="end", part_id="orch-1", observation="done")
    )
    # Accumulated list upserts to a single part carrying the latest phase…
    assert len(emitter.parts) == 1
    assert emitter.parts[0]["phase"] == "end"
    assert emitter.parts[0]["observation"] == "done"
    # …while every emission is still streamed live.
    assert queue.qsize() == 2


async def test_emit_react_part_inherits_parent_from_contextvar():
    queue: asyncio.Queue = asyncio.Queue()
    emitter = ProgressEmitter(queue)

    class _Rec:
        step = 1
        phase = "start"
        thought = "thinking"
        action = "run_script"
        input_preview = "code"
        observation_preview = ""
        call_index = 0

    token = _active_parent_part_id.set("orch-2")
    try:
        await emit_react_part(emitter, "analysis", _Rec(), parent_id=_active_parent_part_id.get())
    finally:
        _active_parent_part_id.reset(token)

    part = emitter.parts[0]
    assert part["id"] == "orch-2-1"
    assert part["parentId"] == "orch-2"
    assert part["agent"] == "analysis"


async def test_emit_part_is_noop_after_finish():
    queue: asyncio.Queue = asyncio.Queue()
    emitter = ProgressEmitter(queue)
    await emitter.finish()
    await emitter.emit_part(
        step_part(agent="orchestrator", step=1, phase="start", part_id="orch-1")
    )
    assert emitter.parts == []

"""
Regression tests for SSE streaming resilience.

Both cases below previously closed the stream with no terminal event and no log,
which the client surfaces as "The response stream ended unexpectedly":
  1. A `result` event whose metadata isn't JSON-serializable was silently dropped.
  2. (Covered structurally in routes.py) worker BaseExceptions bypassed `except
     Exception` so no error event was emitted.
"""

from __future__ import annotations

import asyncio
import json

from helpers.sse import degrade_sse_event, sse_event_generator


class _Unserializable:
    def __repr__(self) -> str:  # make even default=str fail
        raise RuntimeError("cannot stringify")


def test_degrade_preserves_result_answer_when_metadata_unserializable():
    payload = degrade_sse_event(
        {
            "type": "result",
            "response": "Saved 100 rows to the workspace.",
            "status": "completed",
            "metadata": {"trace": _Unserializable()},
        }
    )
    event = json.loads(payload)
    assert event["type"] == "result"
    assert event["response"] == "Saved 100 rows to the workspace."
    assert event["metadata"] == {}


def test_degrade_preserves_non_result_event_type():
    event = json.loads(degrade_sse_event({"type": "error", "message": "boom"}))
    assert event == {"type": "error", "message": "boom"}


async def test_generator_emits_degraded_result_instead_of_dropping():
    """An unserializable result event must still reach the client as a result."""
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(
        {
            "type": "result",
            "response": "Done.",
            "status": "completed",
            "metadata": {"bad": _Unserializable()},
        }
    )
    await queue.put(None)

    async def _noop() -> None:
        return None

    worker = asyncio.create_task(_noop())
    await worker

    chunks = [c async for c in sse_event_generator(queue, worker, heartbeat_interval=1000)]
    events = [json.loads(c.removeprefix("data: ").strip()) for c in chunks]
    results = [e for e in events if e["type"] == "result"]
    assert results, "result event was dropped instead of degraded"
    assert results[0]["response"] == "Done."

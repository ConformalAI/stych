"""Shared SSE streaming plumbing.

Single source of truth for the timeout taxonomy, heartbeat cadence, response
headers, and queue-drain generator used by every SSE endpoint (agent runs,
initial EDA). Legacy per-route copies were removed — import from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SseEventType(str, Enum):
    """Every SSE event type the backend can emit.

    Single source of truth: the frontend union type is generated from this enum
    (see scripts/export_contract_schemas.py) and agentRunStream.ts switches over
    it exhaustively — adding a value here without a frontend handler is a
    compile error, not a silent fall-through.
    """

    THINKING = "thinking"
    PART_UPDATED = "part.updated"
    TOKEN = "token"
    TOKEN_RETRACT = "token_retract"
    THINKING_TOKEN = "thinking_token"
    HEARTBEAT = "heartbeat"
    ERROR = "error"
    RESULT = "result"
    TIMEOUT = "timeout"
    LIMIT_REACHED = "limit_reached"
    CANCELLED = "cancelled"
    RUN_SNAPSHOT = "run_snapshot"
    VIZ = "viz"
    FILTER = "filter"
    ONTOLOGY = "ontology"
    COMPACTION_START = "compaction_start"
    COMPACTION_END = "compaction_end"
    REASONING = "reasoning"


# Authoritative ceiling for a single agent run / stream (15 min). Multi-step
# agentic tasks (fetch → analyse → export) can easily take 10+ minutes when
# paginating large datasets; beyond this the user should break the task up.
# Config may override via server.stream_timeout_seconds — see stream_timeout().
QUERY_TIMEOUT_SECONDS = 900.0

# Idle-queue drain timeout must be ≥ QUERY_TIMEOUT_SECONDS so a slow
# orchestrator step doesn't cause the SSE stream to close before the task ends.
SSE_QUEUE_TIMEOUT = QUERY_TIMEOUT_SECONDS + 60.0

SSE_HEARTBEAT_INTERVAL = 30.0


def stream_timeout(config: dict | None) -> float:
    """Effective run/stream ceiling: config override or QUERY_TIMEOUT_SECONDS."""
    return float(
        (config or {}).get("server", {}).get("stream_timeout_seconds", QUERY_TIMEOUT_SECONDS)
    )


def sse_headers() -> Dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def safe_error_message(exc: BaseException) -> str:
    """Return a client-safe error string — never raw exception internals.

    Full details are always logged server-side before this is called.
    """
    message = str(exc).lower()
    if "openrouter" in message or "502" in message:
        return "AI service is temporarily unavailable. Please try again in a moment."
    if "timeout" in message or isinstance(exc, asyncio.TimeoutError):
        return "The analysis took too long. Try a simpler question or a smaller dataset."
    if "authentication" in message or "api key" in message:
        return "LLM authentication failed. Please check your API key configuration."
    if "quota" in message or "rate limit" in message:
        return "Usage limit reached. Please wait before sending another request."
    return "An error occurred while processing your request. Please try again."


def degrade_sse_event(event: Any) -> str:
    """Build a minimal, guaranteed-serializable SSE payload for an event whose full
    form failed ``json.dumps`` (e.g. non-serializable metadata).

    Preserves the event type and the answer text so a ``result`` is never silently
    lost — only the un-serializable extras (reasoning traces / metadata) are dropped.
    """
    safe: Dict[str, Any] = {
        "type": SseEventType.ERROR.value,
        "message": "Internal serialization error.",
    }
    if isinstance(event, dict):
        etype = event.get("type")
        if etype == SseEventType.RESULT.value:
            safe = {
                "type": SseEventType.RESULT.value,
                "response": str(event.get("response", "")),
                "task_type": str(event.get("task_type", "")),
                "status": str(event.get("status", "completed")),
                "action_prompt": None,
                "metadata": {},
            }
        elif etype:
            safe = {"type": str(etype), "message": str(event.get("message", ""))}
    try:
        return json.dumps(safe, default=str)
    except Exception:
        return json.dumps(
            {"type": SseEventType.ERROR.value, "message": "Internal serialization error."}
        )


async def sse_event_generator(
    queue: asyncio.Queue,
    worker: asyncio.Task,
    *,
    heartbeat_interval: float = SSE_HEARTBEAT_INTERVAL,
    queue_timeout: float = SSE_QUEUE_TIMEOUT,
    timeout_message: str = "Stream timed out.",
    cancel_worker_on_close: bool = True,
):
    """Drain an SSE queue with periodic heartbeats so long LLM steps don't idle-timeout."""
    heartbeat: Optional[asyncio.Task] = None
    try:

        async def _heartbeat_loop() -> None:
            while not worker.done():
                await asyncio.sleep(heartbeat_interval)
                if not worker.done():
                    await queue.put({"type": SseEventType.HEARTBEAT.value})

        heartbeat = asyncio.create_task(_heartbeat_loop())
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=queue_timeout)
            except asyncio.TimeoutError:
                yield (
                    "data: "
                    + json.dumps({"type": SseEventType.ERROR.value, "message": timeout_message})
                    + "\n\n"
                )
                break
            if event is None:
                break
            try:
                payload = json.dumps(event, default=str)
            except Exception:
                # An event field isn't JSON-serializable. Never silently drop it —
                # dropping a `result` event closes the stream with no answer and no
                # log, which the client surfaces as "stream ended unexpectedly".
                # Log it and degrade to a serializable shell that still carries the
                # answer text / event type so the client always gets feedback.
                logger.error(
                    "SSE event of type %r could not be serialized; sending degraded event",
                    event.get("type") if isinstance(event, dict) else type(event).__name__,
                    exc_info=True,
                )
                payload = degrade_sse_event(event)
            yield "data: " + payload + "\n\n"
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        if cancel_worker_on_close and not worker.done():
            worker.cancel()

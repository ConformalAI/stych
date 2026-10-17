"""
Async progress emitter for SSE streaming support.
Agents call emit() to broadcast real-time status events back to the HTTP streaming
endpoint via a shared asyncio.Queue.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from helpers.sse import SseEventType

# Per-request active emitter — set by the route handler so sub-agents can emit
# token events without being passed the emitter explicitly.
# Uses the same ContextVar pattern as _request_model_override in llm/infer.py.
_active_token_emitter: ContextVar[Optional["ProgressEmitter"]] = ContextVar(
    "_active_token_emitter", default=None
)

# Stable id of the orchestrator step whose `call_agent` is currently dispatching a
# sub-agent. Set around the dispatch so the sub-agent's emitted trace parts inherit
# the correct `parentId` without threading it through every call signature.
_active_parent_part_id: ContextVar[Optional[str]] = ContextVar(
    "_active_parent_part_id", default=None
)


_QUEUE_MAX_SIZE = 512  # cap per-request queue to prevent memory growth on slow consumers


class ProgressEmitter:
    """
    Ferries progress events from agents to a StreamingResponse generator.

    Usage in an agent:
        if emitter:
            await emitter.emit("analysis", "Generating analysis code...")

    Usage in the route:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        emitter = ProgressEmitter(queue)
        # start orchestrator task, then drain the queue and yield SSE lines
    """

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue
        self._finished = False
        # Ordered, append-only accumulation of every trace part emitted this turn.
        # Upserted by id so the multiple emissions of one step (start → end) collapse
        # to a single entry. This list IS the canonical trace persisted with the
        # assistant message and used as the live commit fallback on the client.
        self.parts: List[Dict[str, Any]] = []

    async def emit(
        self,
        agent: str,
        message: str,
        event_type: str = SseEventType.THINKING.value,
        **extra: Any,
    ) -> None:
        """Put a progress event on the queue (non-blocking for callers)."""
        if self._finished:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "agent": agent,
            "message": message,
        }
        payload.update(extra)
        try:
            await self._queue.put(payload)
        except asyncio.QueueFull:
            pass  # consumer too slow — drop the event rather than block

    async def emit_part(self, part: Dict[str, Any]) -> None:
        """
        Emit (and accumulate) a trace part. Upserts by ``part["id"]`` so re-emitting
        a step in a later phase replaces the prior entry in place, preserving order.
        """
        if self._finished:
            return
        pid = part.get("id")
        for i, existing in enumerate(self.parts):
            if existing.get("id") == pid:
                self.parts[i] = part
                break
        else:
            self.parts.append(part)
        try:
            await self._queue.put({"type": SseEventType.PART_UPDATED.value, "part": part})
        except asyncio.QueueFull:
            pass  # consumer too slow — drop the live event; the accumulated part survives

    async def emit_token(self, delta: str) -> None:
        """Emit a single text token from the LLM's final-answer stream."""
        if self._finished:
            return
        try:
            await self._queue.put({"type": SseEventType.TOKEN.value, "delta": delta})
        except asyncio.QueueFull:
            pass

    async def emit_stream_retract(self) -> None:
        """
        Tell the client to discard the tokens just streamed via ``emit_token``.

        A ``stream_final`` turn streams live as it's generated, before it's known
        whether the turn ends in a tool call — some models (Claude in particular)
        write a prose preamble before invoking a tool. That preamble isn't the
        final answer; once the tool call appears, this retracts it so the client
        doesn't get stuck treating the response as "already streaming".
        """
        if self._finished:
            return
        try:
            await self._queue.put({"type": SseEventType.TOKEN_RETRACT.value})
        except asyncio.QueueFull:
            pass

    async def emit_thinking_token(self, delta: str) -> None:
        """Emit a chain-of-thought token from a reasoning model (e.g. Qwen3, DeepSeek-R1)."""
        if self._finished:
            return
        try:
            await self._queue.put({"type": SseEventType.THINKING_TOKEN.value, "delta": delta})
        except asyncio.QueueFull:
            pass

    async def finish(self) -> None:
        """Send the None sentinel that signals the generator to close."""
        if self._finished:
            return
        self._finished = True
        await self._queue.put(None)

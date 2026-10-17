"""In-process ownership and replay state for detached agent runs.

The browser may attach to or leave a run at any time.  Execution belongs to the
registry, while SSE consumers only observe the event buffer.  This intentionally
does not survive a backend process restart; the persisted conversation remains the
durable record of completed runs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Optional

from helpers.sse import SseEventType

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "timed_out", "limit_reached"}
ACTIVE_RUN_STATUSES = {"queued", "running"}
_MAX_REPLAY_EVENTS = 2048
_MAX_SNAPSHOT_ARTIFACT_EVENTS = 128
_TERMINAL_RETENTION = timedelta(hours=1)


class ActiveSessionRunError(RuntimeError):
    """Raised when a session already owns a queued or running request."""


class ConcurrentRunLimitError(RuntimeError):
    """Raised when a user has reached the configured active-run limit."""


class RunEventBuffer:
    """Bounded event replay plus a lossless snapshot of current live UI state."""

    def __init__(self, max_events: int = _MAX_REPLAY_EVENTS) -> None:
        self._events: Deque[tuple[int, Dict[str, Any]]] = deque(maxlen=max_events)
        self._condition = asyncio.Condition()
        self._sequence = 0
        self.streaming_content: Optional[str] = None
        self.thinking_content: Optional[str] = None
        self.thinking_agent: Optional[Dict[str, str]] = None
        self.parts: list[Dict[str, Any]] = []
        self.artifact_events: list[Dict[str, Any]] = []
        self.result: Optional[Dict[str, Any]] = None
        self.last_error: Optional[Dict[str, Any]] = None

    async def put(self, event: Optional[Dict[str, Any]]) -> None:
        """Queue-compatible sink used by ``ProgressEmitter`` without backpressure."""
        if event is None:
            async with self._condition:
                self._condition.notify_all()
            return

        async with self._condition:
            self._sequence += 1
            stored = dict(event)
            stored["sequence"] = self._sequence
            self._events.append((self._sequence, stored))
            self._update_snapshot(stored)
            self._condition.notify_all()

    def _update_snapshot(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == SseEventType.TOKEN.value:
            if self.streaming_content is None:
                self.streaming_content = ""
            self.streaming_content += str(event.get("delta") or "")
        elif event_type == SseEventType.TOKEN_RETRACT.value:
            self.streaming_content = None
        elif event_type == SseEventType.THINKING_TOKEN.value:
            if self.thinking_content is None:
                self.thinking_content = ""
            self.thinking_content += str(event.get("delta") or "")
        elif event_type == SseEventType.THINKING.value:
            self.thinking_agent = {
                "agent": str(event.get("agent") or ""),
                "message": str(event.get("message") or ""),
            }
        elif event_type == SseEventType.PART_UPDATED.value and isinstance(event.get("part"), dict):
            part = event["part"]
            part_id = part.get("id")
            for index, existing in enumerate(self.parts):
                if existing.get("id") == part_id:
                    self.parts[index] = part
                    break
            else:
                self.parts.append(part)
        elif event_type in {SseEventType.VIZ.value, SseEventType.FILTER.value}:
            self.artifact_events.append(event)
            self.artifact_events = self.artifact_events[-_MAX_SNAPSHOT_ARTIFACT_EVENTS:]
        elif event_type == SseEventType.RESULT.value:
            self.result = event
        elif event_type in {
            SseEventType.ERROR.value,
            SseEventType.TIMEOUT.value,
            SseEventType.LIMIT_REACHED.value,
            SseEventType.CANCELLED.value,
        }:
            self.last_error = event

    async def snapshot(self, run: "AgentRun") -> Dict[str, Any]:
        async with self._condition:
            return self._snapshot_locked(run)

    def _snapshot_locked(self, run: "AgentRun") -> Dict[str, Any]:
        return {
            "type": SseEventType.RUN_SNAPSHOT.value,
            "run_id": run.id,
            "session_id": run.session_id,
            "status": run.status,
            "sequence": self._sequence,
            "streaming_content": self.streaming_content,
            "thinking_content": self.thinking_content,
            "thinking_agent": self.thinking_agent,
            "parts": list(self.parts),
            "artifact_events": list(self.artifact_events),
            "result": self.result,
            "terminal_event": self.last_error,
        }

    async def wait_after(
        self,
        sequence: int,
        run: "AgentRun",
        timeout: float,
    ) -> tuple[list[Dict[str, Any]], str]:
        async with self._condition:
            if self._sequence <= sequence and run.status not in TERMINAL_RUN_STATUSES:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(
                            lambda: self._sequence > sequence or run.status in TERMINAL_RUN_STATUSES
                        ),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return [], run.status
            events = [event for seq, event in self._events if seq > sequence]
            return events, run.status

    async def notify_status_changed(self) -> None:
        async with self._condition:
            self._condition.notify_all()


@dataclass
class AgentRun:
    id: str
    session_id: str
    user_id: str
    client_request_id: Optional[str] = None
    status: str = "queued"
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancel_requested: bool = False
    task: Optional[asyncio.Task] = None
    events: RunEventBuffer = field(default_factory=RunEventBuffer)

    async def set_status(self, status: str) -> None:
        self.status = status
        now = datetime.utcnow()
        if status == "running" and self.started_at is None:
            self.started_at = now
        if status in TERMINAL_RUN_STATUSES:
            self.completed_at = now
        await self.events.notify_status_changed()

    def public_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class AgentRunRegistry:
    """Process-local run registry keyed by stable run IDs."""

    def __init__(self) -> None:
        self._runs: Dict[str, AgentRun] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        session_id: str,
        user_id: str,
        client_request_id: Optional[str] = None,
    ) -> AgentRun:
        async with self._lock:
            self._prune_locked()
            if client_request_id:
                for existing in self._runs.values():
                    if (
                        existing.user_id == user_id
                        and existing.session_id == session_id
                        and existing.client_request_id == client_request_id
                    ):
                        return existing
            run = AgentRun(
                id=str(uuid.uuid4()),
                session_id=session_id,
                user_id=user_id,
                client_request_id=client_request_id,
            )
            self._runs[run.id] = run
            return run

    async def admit(
        self,
        session_id: str,
        user_id: str,
        client_request_id: Optional[str],
        *,
        max_active: int,
    ) -> tuple[AgentRun, bool]:
        """Atomically enforce idempotency and active-run admission.

        Returns ``(run, created)``. An idempotent retry receives the existing
        run with ``created=False`` and therefore must not launch another task.
        """
        async with self._lock:
            self._prune_locked()
            if client_request_id:
                duplicate = next(
                    (
                        run
                        for run in self._runs.values()
                        if run.user_id == user_id
                        and run.session_id == session_id
                        and run.client_request_id == client_request_id
                    ),
                    None,
                )
                if duplicate is not None:
                    return duplicate, False

            if any(
                run.user_id == user_id
                and run.session_id == session_id
                and run.status in ACTIVE_RUN_STATUSES
                for run in self._runs.values()
            ):
                raise ActiveSessionRunError(session_id)

            active_count = sum(
                1
                for run in self._runs.values()
                if run.user_id == user_id and run.status in ACTIVE_RUN_STATUSES
            )
            if active_count >= max(1, max_active):
                raise ConcurrentRunLimitError(user_id)

            run = AgentRun(
                id=str(uuid.uuid4()),
                session_id=session_id,
                user_id=user_id,
                client_request_id=client_request_id,
            )
            self._runs[run.id] = run
            return run, True

    def get(self, run_id: str, user_id: str) -> Optional[AgentRun]:
        run = self._runs.get(run_id)
        return run if run is not None and run.user_id == user_id else None

    def find_by_client_request(
        self,
        session_id: str,
        user_id: str,
        client_request_id: Optional[str],
    ) -> Optional[AgentRun]:
        if not client_request_id:
            return None
        return next(
            (
                run
                for run in self._runs.values()
                if run.user_id == user_id
                and run.session_id == session_id
                and run.client_request_id == client_request_id
            ),
            None,
        )

    def latest_for_session(self, session_id: str, user_id: str) -> Optional[AgentRun]:
        matches = [
            run
            for run in self._runs.values()
            if run.session_id == session_id and run.user_id == user_id
        ]
        return max(matches, key=lambda run: run.created_at) if matches else None

    def active_count(self, user_id: str) -> int:
        return sum(
            1
            for run in self._runs.values()
            if run.user_id == user_id and run.status in ACTIVE_RUN_STATUSES
        )

    def active_for_session(self, session_id: str, user_id: str) -> Optional[AgentRun]:
        run = self.latest_for_session(session_id, user_id)
        return run if run is not None and run.status in ACTIVE_RUN_STATUSES else None

    def _prune_locked(self) -> None:
        cutoff = datetime.utcnow() - _TERMINAL_RETENTION
        expired = [
            run_id
            for run_id, run in self._runs.items()
            if run.completed_at is not None and run.completed_at < cutoff
        ]
        for run_id in expired:
            self._runs.pop(run_id, None)


agent_run_registry = AgentRunRegistry()

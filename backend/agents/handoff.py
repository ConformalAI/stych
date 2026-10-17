"""
Session-level handoff events between agents (orchestrator + sub-agents).

Persists a small append-only list on ``AgentState.metadata`` so the next step
(another subagent or the orchestrator) can see what was done and which
artifacts to use. Keep payloads JSON-serializable and compact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Single key to avoid ad hoc metadata sprawl. Value shape:
# { "events": [ {...}, ... ], "latest": { ... } | None }
STYCH_HANDOFF_KEY = "stych_agent_handoff"


def append_handoff_event(
    state: Optional[Any],
    *,
    source_agent: str,
    action: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one handoff for downstream agents and the orchestrator UI."""
    if not state or not state.metadata:
        return
    payload: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source_agent": source_agent,
        "action": action,
    }
    if details:
        payload["details"] = {k: v for k, v in details.items() if v is not None}

    bucket = state.metadata.get(STYCH_HANDOFF_KEY)
    if not isinstance(bucket, dict):
        bucket = {"events": []}
    events: List[Dict[str, Any]] = list(bucket.get("events") or [])
    # Cap history so sessions don't grow without bound
    if len(events) > 100:
        events = events[-100:]
    events.append(payload)
    bucket = {"events": events, "latest": payload}
    state.metadata[STYCH_HANDOFF_KEY] = bucket
    logger.debug(
        "[handoff] source=%s action=%s keys=%s",
        source_agent,
        action,
        list((details or {}).keys()) if details else [],
    )


def get_latest_handoff(state: Optional[Any]) -> Optional[Dict[str, Any]]:
    if not state or not state.metadata:
        return None
    bucket = state.metadata.get(STYCH_HANDOFF_KEY)
    if isinstance(bucket, dict) and isinstance(bucket.get("latest"), dict):
        return bucket["latest"]
    return None


def handoff_to_response_metadata(state: Optional[Any]) -> Dict[str, Any]:
    """
    Shallow copy for subagent return metadata / orchestration traces.
    """
    if not state or not state.metadata:
        return {}
    h = state.metadata.get(STYCH_HANDOFF_KEY)
    if isinstance(h, dict) and h.get("latest"):
        return {"stych_handoff": dict(h.get("latest") or {})}
    return {}

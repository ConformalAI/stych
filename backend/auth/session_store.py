"""
Volatile in-memory session store.

Intentionally NOT persisted to disk or database.
On every server restart the store starts empty, which means every
browser-held JWT (whose session_id is no longer registered) will
receive a 401 on the next request and the frontend will redirect to login.

On explicit logout the session is revoked immediately so the cookie
cannot be replayed even within its TTL window.
"""

from __future__ import annotations

import logging
import threading
from typing import Set

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_active_sessions: Set[str] = set()


def register_session(session_id: str) -> None:
    """Register a new session after successful login."""
    with _lock:
        _active_sessions.add(session_id)
    logger.debug("[SessionStore] Registered session: %s", session_id[:8])


def is_session_valid(session_id: str) -> bool:
    """Return True only if the session was registered in this server process."""
    with _lock:
        return session_id in _active_sessions


def revoke_session(session_id: str) -> None:
    """Invalidate a session immediately (called on logout)."""
    with _lock:
        _active_sessions.discard(session_id)
    logger.debug("[SessionStore] Revoked session: %s", session_id[:8])


def active_count() -> int:
    """Return number of currently active sessions (for diagnostics)."""
    with _lock:
        return len(_active_sessions)

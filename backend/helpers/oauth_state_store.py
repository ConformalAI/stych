"""
Database-backed OAuth CSRF state for MCP integration flows.

Survives multi-worker deployments and uvicorn --reload (unlike in-memory dicts).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from database.models import OAuthPendingState

logger = logging.getLogger(__name__)


def prune_expired_oauth_states(db: Session) -> int:
    """Delete expired pending OAuth states. Returns rows removed."""
    now = datetime.utcnow()
    deleted = (
        db.query(OAuthPendingState)
        .filter(OAuthPendingState.expires_at < now)
        .delete(synchronize_session=False)
    )
    if deleted:
        db.commit()
        logger.debug("[oauth] pruned %d expired state(s)", deleted)
    return deleted


def create_oauth_state(
    db: Session,
    app_user_id: str,
    server_id: str,
    *,
    ttl_seconds: int = 600,
) -> str:
    """Create a one-time OAuth state token bound to user + server."""
    prune_expired_oauth_states(db)
    state = secrets.token_urlsafe(32)
    row = OAuthPendingState(
        state=state,
        app_user_id=app_user_id,
        server_id=server_id,
        expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    return state


def consume_oauth_state(db: Session, state: str) -> Optional[Dict[str, Any]]:
    """
    Validate and consume a pending OAuth state (one-time use).

    Returns ``{user_id, server_id}`` or None if missing/expired.
    """
    row: Optional[OAuthPendingState] = (
        db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
    )
    if row is None:
        return None

    db.delete(row)
    db.commit()

    if row.expires_at < datetime.utcnow():
        logger.warning("[oauth] expired state consumed server=%s", row.server_id)
        return None

    return {"user_id": row.app_user_id, "server_id": row.server_id}

"""
Session policy guards shared across authenticated routes.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import HTTPException, status
from supertokens_python.recipe.session import SessionContainer

logger = logging.getLogger(__name__)

# Server boot epoch seconds; used to reject pre-restart sessions when enabled.
_PROCESS_START_EPOCH = int(time.time())


def _is_restart_invalidation_enabled() -> bool:
    raw = os.getenv("INVALIDATE_SESSIONS_ON_RESTART")
    if raw is not None:
        return raw.strip().lower() == "true"
    # Safe default: enabled in non-production, disabled in production.
    return os.getenv("ENV", "development").strip().lower() != "production"


def enforce_session_restart_policy(session: SessionContainer) -> None:
    """
    Reject sessions minted before this backend process started.
    """
    if not _is_restart_invalidation_enabled():
        return

    payload = {}
    try:
        payload = session.get_access_token_payload() or {}
    except Exception:
        payload = {}

    # SuperTokens / JWT ecosystems may represent issue-time in different keys:
    # - st-iat: original session creation timestamp (stable across access-token refresh)
    # - iat: access-token issue timestamp (changes on refresh)
    # To enforce "logout on backend restart", prefer st-iat first so refresh
    # cannot bypass restart invalidation.
    iat_raw = payload.get("st-iat")
    if iat_raw is None:
        iat_raw = payload.get("iat")
    try:
        issued_at = int(float(iat_raw))
    except Exception:
        issued_at = 0

    # Normalize milliseconds epoch to seconds.
    if issued_at > 10_000_000_000:
        issued_at = issued_at // 1000

    if issued_at < _PROCESS_START_EPOCH:
        logger.info(
            "Session rejected by restart policy (iat=%s < boot=%s)",
            issued_at,
            _PROCESS_START_EPOCH,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired after server restart. Please sign in again.",
        )

"""
FastAPI dependencies for dependency injection.
"""

import hashlib
import logging
import os
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from agents.state import state_manager
from api.session_policy import enforce_session_restart_policy
from database.app_user_store import (
    get_active_app_user_from_st_id,
    upsert_app_user_from_supertokens,
)
from database.connection import get_db
from database.models import AppUser

logger = logging.getLogger(__name__)

# These will be injected at app startup
_orchestrator = None
_a2a_protocol = None
_config = None


def _token_fingerprint(token: Optional[str]) -> str:
    """Return a short, non-reversible token fingerprint for log correlation."""
    if not token:
        return "none"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _request_debug_context(request: Request) -> str:
    """Format request metadata useful for auth/cookie debugging."""
    cookie_names = ",".join(sorted(request.cookies.keys())) or "none"
    forwarded_proto = request.headers.get("x-forwarded-proto", "unset")
    referer = request.headers.get("referer", "unset")
    if referer != "unset":
        try:
            parts = urlsplit(referer)
            # Drop query/fragment to avoid leaking sensitive URL params.
            referer = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            referer = "malformed"
        referer = referer[:256]
    return (
        f"client={request.client.host if request.client else 'unknown'} "
        f"method={request.method} path={request.url.path} "
        f"origin={request.headers.get('origin', 'unset')} "
        f"referer={referer} "
        f"host={request.headers.get('host', 'unset')} "
        f"x_forwarded_proto={forwarded_proto} "
        f"cookie_header_present={'cookie' in request.headers} "
        f"cookie_names={cookie_names}"
    )


def set_dependencies(orchestrator, a2a_protocol, config):
    """Set global dependencies (called at app startup)."""
    global _orchestrator, _a2a_protocol, _config
    _orchestrator = orchestrator
    _a2a_protocol = a2a_protocol
    _config = config


def resolve_session(session_id: str, user: str, *, is_anonymous: bool = False):
    """Load or create a session, mapping ownership conflicts to HTTP 403."""
    try:
        return state_manager.get_or_create_session(
            session_id,
            app_user_id=user,
            is_anonymous=is_anonymous,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Session belongs to another user.")


def get_orchestrator():
    """Get orchestrator agent instance."""
    if _orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator not initialized",
        )
    return _orchestrator


def get_a2a_protocol():
    """Get A2A protocol instance."""
    return _a2a_protocol


def get_config():
    """Get application configuration."""
    if _config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configuration not loaded",
        )
    return _config


def _parse_csv_env(var_name: str) -> set[str]:
    """Parse a comma-separated env var into a set of normalized values."""
    raw = os.getenv(var_name, "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


async def get_current_user_record(
    request: Request,
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
) -> AppUser:
    """
    Get the current authenticated app-user row via SuperTokens session.
    """
    enforce_session_restart_policy(session)
    st_user_id = session.get_user_id()
    context = _request_debug_context(request)

    user = get_active_app_user_from_st_id(db, st_user_id)
    if not user:
        # Self-heal bridge rows for sessions where callback ran before app DB write
        # completed (or during migration windows).
        try:
            payload = session.get_access_token_payload() or {}
            email = payload.get("email") or payload.get("st-email") or ""
            if isinstance(email, dict):
                email = email.get("value", "")
            if not email:
                logger.warning(
                    "Auth failed: no app_user row and no email claim st_id=%s (%s)",
                    st_user_id[:8],
                    context,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not provisioned in app DB",
                )
            user = upsert_app_user_from_supertokens(
                db,
                st_user_id=st_user_id,
                email=str(email),
            )
            db.commit()
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            logger.warning("Auth failed: no app user for st_id=%s (%s)", st_user_id[:8], context)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found — please sign in again",
            )

    if not user.is_active:
        logger.warning("Auth failed: inactive user user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    logger.debug("Auth success: user_id=%s st_id=%s", user.id, st_user_id[:8])
    return user


async def get_current_user(
    user: AppUser = Depends(get_current_user_record),
) -> str:
    """
    Get current authenticated user via SuperTokens session.

    SuperTokens verify_session() validates the session cookie and raises a
    401 automatically if the session is missing or expired.  We then look up
    the matching app user by supertokens_user_id.

    Returns:
        App user ID (UUID string)

    Raises:
        HTTPException 401/403 if session is invalid or user is inactive
    """
    return user.id


async def get_current_admin_user(
    user: AppUser = Depends(get_current_user_record),
) -> AppUser:
    """
    Require an authenticated admin user.

    Admin grant sources (checked in order):
    - `ADMIN_USER_IDS` env: comma-separated Stych user UUIDs
    - `ADMIN_EMAILS` env: comma-separated email addresses
    - `AppUser.is_admin` column: forward-compatible path for a role column
      that does not yet exist in the schema (always evaluates to False today).
    """
    # Forward-compatible guard: if a future migration adds an is_admin column,
    # this path will start working without a code change.
    if bool(getattr(user, "is_admin", False)):
        return user

    allowed_user_ids = _parse_csv_env("ADMIN_USER_IDS")
    allowed_emails = _parse_csv_env("ADMIN_EMAILS")

    if user.id.lower() in allowed_user_ids:
        return user
    if (user.email or "").strip().lower() in allowed_emails:
        return user

    logger.warning("Forbidden admin action attempted by user_id=%s", user.id)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin privileges required",
    )

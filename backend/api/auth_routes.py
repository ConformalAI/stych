"""
Auth routes for Stych API.

SuperTokens automatically mounts all sign-up / sign-in / sign-out / session-refresh
routes under /auth/* (configured in supertokens_config.py).  This file only adds:

  GET  /api/auth/me      — return current user profile (ST session required)
  POST /api/auth/dev     — dev-only instant login (DEV_MODE=true only)

The old /api/auth/google and /api/auth/logout endpoints are replaced by the
SuperTokens SDK routes:
  POST /auth/signin                  (email + password)
  POST /auth/signup                  (email + password)
  POST /auth/signout                 (any session)
  GET  /auth/authorisationurl        (start Google OAuth)
  GET  /auth/callback/google         (Google OAuth callback)
  GET  /auth/session/refresh         (rotate tokens)
  GET  /auth/dashboard               (user management UI — secured by ST API key)
"""

import base64
import csv
import io
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from api.dependencies import get_current_user
from api.session_policy import enforce_session_restart_policy
from auth.models import (
    AuthResponse,
    ChangePasswordRequest,
    SessionListResponse,
    SessionSummaryResponse,
    UpdateUserRequest,
    UsageEventResponse,
    UsageEventsPageResponse,
    UsageSessionListResponse,
    UsageSummaryResponse,
    UserResponse,
)
from database.app_user_store import (
    get_active_app_user_from_st_id,
    is_valid_handle,
    normalize_handle,
    upsert_app_user_from_supertokens,
)
from database.connection import get_db
from database.models import AppUser, LLMUsageEvent, SessionHistory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["authentication"])
dev_router = APIRouter(prefix="/api/auth", tags=["authentication"])
session_router = APIRouter(prefix="/api", tags=["sessions"])


async def _session_user_can_change_password(st_user_id: str) -> bool:
    """
    Return True when the account has an EmailPassword login method.
    """
    try:
        from supertokens_python.asyncio import get_user
    except Exception:
        return False

    try:
        st_user = await get_user(st_user_id)
        if st_user is None:
            return False
        login_methods = getattr(st_user, "login_methods", []) or []
        return any(
            getattr(method, "recipe_id", "").lower() == "emailpassword" for method in login_methods
        )
    except Exception as exc:
        logger.warning("Failed to inspect SuperTokens login methods for %s: %s", st_user_id, exc)
        return False


def _to_user_response(user: AppUser, can_change_password: bool) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        handle=user.handle,
        name=user.name,
        profile_picture=user.profile_picture,
        is_active=user.is_active,
        can_change_password=can_change_password,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


def _get_active_user_from_session(session: SessionContainer, db: Session) -> tuple[AppUser, str]:
    enforce_session_restart_policy(session)
    st_user_id = session.get_user_id()
    user = get_active_app_user_from_st_id(db, st_user_id)
    if not user:
        payload = session.get_access_token_payload() or {}
        email = payload.get("email") or payload.get("st-email") or ""
        if not email:
            raise HTTPException(status_code=404, detail="User not found")
        user = upsert_app_user_from_supertokens(
            db,
            st_user_id=st_user_id,
            email=str(email),
        )
        db.commit()
        db.refresh(user)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")
    return user, st_user_id


# ---------------------------------------------------------------------------
# /api/auth/me
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    """
    Return the currently authenticated user's profile.

    SuperTokens verify_session() raises 401 automatically if the session
    cookie is absent or expired — no manual cookie handling needed.
    """
    user, st_user_id = _get_active_user_from_session(session, db)

    can_change_password = await _session_user_can_change_password(st_user_id)
    return _to_user_response(user, can_change_password)


@router.patch("/me", response_model=UserResponse)
async def update_current_user_info(
    payload: UpdateUserRequest,
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    """
    Update editable user profile fields for the current authenticated user.
    """
    user, st_user_id = _get_active_user_from_session(session, db)

    if payload.handle is not None:
        raw_handle = payload.handle.strip()
        if raw_handle:
            handle = normalize_handle(raw_handle)
            # Skip validation + uniqueness check when the user is keeping their
            # existing handle unchanged (avoids 400 for any handle that was
            # auto-generated before the 3-char minimum was enforced).
            if handle != user.handle:
                if not is_valid_handle(handle):
                    raise HTTPException(
                        status_code=400,
                        detail="Handle must be 3-50 chars and use only letters, numbers, '_' or '-'",
                    )
                existing = (
                    db.query(AppUser)
                    .filter(AppUser.handle == handle, AppUser.id != user.id)
                    .first()
                )
                if existing:
                    raise HTTPException(status_code=409, detail="Handle is already taken")
            user.handle = handle
        else:
            user.handle = None

    if payload.name is not None:
        name = payload.name.strip()
        user.name = name or None

    if payload.profile_picture is not None:
        picture = payload.profile_picture.strip()
        if picture:
            # Only allow base64 data URIs (same as the avatar upload produces)
            # or HTTPS URLs — block javascript: and other dangerous schemes.
            if not (picture.startswith("data:image/") or picture.startswith("https://")):
                raise HTTPException(
                    status_code=400,
                    detail="profile_picture must be a data:image/ URI or an https:// URL",
                )
        user.profile_picture = picture or None

    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    can_change_password = await _session_user_can_change_password(st_user_id)
    return _to_user_response(user, can_change_password)


# ---------------------------------------------------------------------------
# /api/auth/avatar  — upload profile image, resize, store as data URI
# ---------------------------------------------------------------------------

_AVATAR_MAX_BYTES = 5 * 1024 * 1024  # 5 MB raw upload limit
_AVATAR_SIZE = (256, 256)  # max output dimensions


@router.post("/avatar", response_model=UserResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    """
    Accept a multipart image upload, resize to 256×256, store as a
    base64 data URI in profile_picture (no separate table needed —
    Postgres TEXT column holds the data URI string fine).
    """
    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(status_code=500, detail="Image processing library not available")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image (JPEG, PNG, WebP, …)")

    # Read in 64 KB chunks with an early size guard to prevent OOM on large uploads.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > _AVATAR_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Image must be smaller than 5 MB")
        chunks.append(chunk)
    raw = b"".join(chunks)

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail(_AVATAR_SIZE, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=82, method=4)
        data_uri = "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        logger.warning("Avatar processing failed: %s", exc)
        raise HTTPException(status_code=400, detail="Could not process image file")

    user, st_user_id = _get_active_user_from_session(session, db)
    user.profile_picture = data_uri
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    can_change_password = await _session_user_can_change_password(st_user_id)
    return _to_user_response(user, can_change_password)


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    """
    Change password for users that signed in via EmailPassword.
    """
    user, st_user_id = _get_active_user_from_session(session, db)

    can_change_password = await _session_user_can_change_password(st_user_id)
    if not can_change_password:
        raise HTTPException(
            status_code=400,
            detail="Password change is only available for email/password accounts",
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=400,
            detail="New password must be different from current password",
        )

    try:
        from supertokens_python.recipe.emailpassword.asyncio import (
            sign_in as st_sign_in,
        )
        from supertokens_python.recipe.emailpassword.asyncio import (
            update_email_or_password,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Password service unavailable")

    # Verify current password first.
    try:
        sign_in_res = await st_sign_in(
            tenant_id="public",
            email=user.email,
            password=payload.current_password,
        )
    except TypeError:
        # Compatibility fallback for older SDK signatures.
        sign_in_res = await st_sign_in(user.email, payload.current_password)
    except Exception as exc:
        logger.error("Password verification failed for user %s: %s", user.id, exc)
        raise HTTPException(status_code=500, detail="Could not verify current password")

    if getattr(sign_in_res, "status", "") != "OK":
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Locate emailpassword recipe user id (handles account-linking cases).
    recipe_user_id = st_user_id
    try:
        from supertokens_python.asyncio import get_user

        st_user = await get_user(st_user_id)
        if st_user is not None:
            login_methods = getattr(st_user, "login_methods", []) or []
            ep_login = next(
                (
                    m
                    for m in login_methods
                    if getattr(m, "recipe_id", "").lower() == "emailpassword"
                ),
                None,
            )
            if ep_login is not None:
                recipe_user_id = getattr(ep_login, "recipe_user_id", st_user_id)
    except Exception:
        pass

    try:
        update_res = await update_email_or_password(
            recipe_user_id,
            password=payload.new_password,
        )
    except Exception as exc:
        logger.error("Failed to update password for user %s: %s", user.id, exc)
        raise HTTPException(status_code=500, detail="Failed to update password")

    if getattr(update_res, "status", "") != "OK":
        raise HTTPException(status_code=400, detail="Unable to update password")

    return {"message": "Password updated successfully"}


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    days: int = Query(default=30, ge=1, le=365),
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    """
    Credit and token consumption summary sourced from LLM usage telemetry.
    """
    user, _ = _get_active_user_from_session(session, db)

    from llm.usage_tracker import get_usage_tracker

    summary = get_usage_tracker().get_summary(user_id=user.id, days=days)
    return UsageSummaryResponse(**summary)


@router.get("/usage/sessions", response_model=UsageSessionListResponse)
async def usage_sessions(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    user, _ = _get_active_user_from_session(session, db)

    from llm.usage_tracker import get_usage_tracker

    data = get_usage_tracker().get_session_summaries(user_id=user.id, days=days, limit=limit)
    return UsageSessionListResponse(**data)


@session_router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    days: int | None = Query(default=None, ge=1, le=365),
    before: datetime | None = Query(default=None),
    before_id: str | None = Query(default=None),
    user: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List durable sessions newest-first, with keyset pagination.

    ``days`` is optional. When omitted, history is unbounded. The sidebar
    sends no window so every session can load in chunks.
    """
    cutoff = datetime.utcnow() - timedelta(days=days) if days else None

    usage_filters = [
        LLMUsageEvent.user_id == user,
        LLMUsageEvent.session_id.isnot(None),
    ]
    if cutoff is not None:
        usage_filters.append(LLMUsageEvent.created_at >= cutoff)
    usage = (
        db.query(
            LLMUsageEvent.session_id.label("session_id"),
            func.count(LLMUsageEvent.id).label("request_count"),
            func.coalesce(func.sum(LLMUsageEvent.estimated_cost_usd), 0.0).label(
                "estimated_cost_usd"
            ),
            func.coalesce(func.sum(LLMUsageEvent.credits_consumed), 0.0).label("credits_consumed"),
        )
        .filter(*usage_filters)
        .group_by(LLMUsageEvent.session_id)
        .subquery()
    )

    activity = func.coalesce(
        SessionHistory.last_activity_at,
        SessionHistory.updated_at,
        SessionHistory.created_at,
    )
    query = (
        db.query(
            SessionHistory,
            usage.c.request_count,
            usage.c.estimated_cost_usd,
            usage.c.credits_consumed,
        )
        .outerjoin(usage, usage.c.session_id == SessionHistory.id)
        .filter(
            SessionHistory.app_user_id == user,
            SessionHistory.status == "active",
        )
    )
    if cutoff is not None:
        query = query.filter(activity >= cutoff)
    if before is not None:
        cursor_time = before.replace(tzinfo=None) if before.tzinfo else before
        if before_id:
            query = query.filter(
                or_(
                    activity < cursor_time,
                    and_(activity == cursor_time, SessionHistory.id < before_id),
                )
            )
        else:
            query = query.filter(activity < cursor_time)

    rows = query.order_by(activity.desc(), SessionHistory.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    summaries = []
    for row, request_count, estimated_cost_usd, credits_consumed in rows:
        activity_at = row.last_activity_at or row.updated_at or row.created_at
        summaries.append(
            SessionSummaryResponse(
                session_id=row.id,
                title=row.title,
                created_at=row.created_at.isoformat() if row.created_at else None,
                last_activity_at=activity_at.isoformat() if activity_at else None,
                request_count=int(request_count or 0),
                total_tokens=int(row.total_tokens or 0),
                estimated_cost_usd=float(estimated_cost_usd or 0.0),
                credits_consumed=float(credits_consumed or 0.0),
                is_anonymous=bool(row.is_anonymous),
            )
        )

    next_before = None
    next_before_id = None
    if has_more and summaries:
        next_before = summaries[-1].last_activity_at
        next_before_id = summaries[-1].session_id

    return SessionListResponse(
        window_days=days,
        sessions=summaries,
        has_more=has_more,
        next_before=next_before,
        next_before_id=next_before_id,
    )


@router.get("/usage/events", response_model=UsageEventsPageResponse)
async def usage_events(
    days: int = Query(default=30, ge=1, le=365),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: str = Query(
        default="created_at",
        pattern="^(created_at|total_tokens|credits_consumed|estimated_cost_usd)$",
    ),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    user, _ = _get_active_user_from_session(session, db)

    from llm.usage_tracker import get_usage_tracker

    data = get_usage_tracker().list_events(
        user_id=user.id,
        days=days,
        page=page,
        page_size=page_size,
        sort_by=sort_by,  # validated by regex
        sort_order=sort_order,  # validated by regex
    )
    return UsageEventsPageResponse(**data)


@router.get("/usage/events/item/{event_id}", response_model=UsageEventResponse)
async def usage_event_detail(
    event_id: str,
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    user, _ = _get_active_user_from_session(session, db)

    from llm.usage_tracker import get_usage_tracker

    event = get_usage_tracker().get_event(user_id=user.id, event_id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Usage event not found")
    return UsageEventResponse(**event)


@router.get("/usage/events/export.csv")
async def usage_events_export_csv(
    days: int = 30,
    sort_by: str = Query(
        default="created_at",
        pattern="^(created_at|total_tokens|credits_consumed|estimated_cost_usd)$",
    ),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: SessionContainer = Depends(verify_session()),
    db: Session = Depends(get_db),
):
    user, _ = _get_active_user_from_session(session, db)

    from llm.usage_tracker import get_usage_tracker

    # Export the latest 5000 rows in selected sort order.
    data = get_usage_tracker().list_events(
        user_id=user.id,
        days=days,
        page=1,
        page_size=5000,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "created_at",
            "session_id",
            "provider",
            "model",
            "agent_name",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "credits_consumed",
        ]
    )
    for row in data.get("items", []):
        writer.writerow(
            [
                row.get("created_at"),
                row.get("session_id"),
                row.get("provider"),
                row.get("model"),
                row.get("agent_name"),
                row.get("latency_ms"),
                row.get("prompt_tokens"),
                row.get("completion_tokens"),
                row.get("total_tokens"),
                row.get("estimated_cost_usd"),
                row.get("credits_consumed"),
            ]
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage_events.csv"'},
    )


# ---------------------------------------------------------------------------
# /api/auth/dev  (DEV_MODE only)
# ---------------------------------------------------------------------------


@dev_router.post("/dev", response_model=AuthResponse)
async def dev_auth(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Development-only instant login.

    Creates (or reuses) a dev user and programmatically creates a SuperTokens
    session for them.  SuperTokens middleware intercepts the outgoing response
    and injects the session cookies automatically.

    Only active when ENV != production AND DEV_MODE=true.
    """
    from supertokens_python.recipe.session.asyncio import create_new_session

    from auth.dev_auth import get_or_create_dev_user, is_dev_mode_allowed

    if not is_dev_mode_allowed():
        raise HTTPException(
            status_code=403,
            detail="Dev mode authentication is disabled (production or DEV_MODE=false)",
        )

    user = get_or_create_dev_user(db)
    if not user:
        raise HTTPException(status_code=500, detail="Failed to create dev user")

    # Ensure the dev user has a supertokens_user_id so verify_session can find them
    if not user.supertokens_user_id:
        user.supertokens_user_id = f"stych_dev_{user.email}"
        db.commit()
        db.refresh(user)

    # Create a SuperTokens session programmatically.
    # The ST middleware attaches the resulting session cookies to the response.
    try:
        from supertokens_python.types import RecipeUserId

        recipe_user_id = RecipeUserId(user.supertokens_user_id)
    except Exception:
        # Backward-compatible shim for SDK variants where RecipeUserId import path
        # differs but create_new_session still expects get_as_string().
        class _RecipeUserIdShim:
            def __init__(self, value: str):
                self._value = value

            def get_as_string(self) -> str:
                return self._value

        recipe_user_id = _RecipeUserIdShim(user.supertokens_user_id)

    await create_new_session(request, "public", recipe_user_id)

    logger.info("Dev auth: session created for user_id=%s email=%s", user.id, user.email)

    return AuthResponse(
        message="Dev mode authentication successful",
        user=UserResponse(
            id=user.id,
            email=user.email,
            handle=user.handle,
            name=user.name,
            profile_picture=user.profile_picture,
            is_active=user.is_active,
            can_change_password=False,
            created_at=user.created_at.isoformat() if user.created_at else None,
        ),
    )

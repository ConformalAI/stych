"""
App-user bridge helpers.

All runtime auth-to-app mapping should resolve via app_users.supertokens_user_id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import AppUser, UserQuota


def normalize_handle(value: str) -> str:
    return value.strip().lower()


def is_valid_handle(value: str) -> bool:
    if not (3 <= len(value) <= 50):
        return False
    return all(ch.isalnum() or ch in {"_", "-"} for ch in value)


def generate_unique_handle(db: Session, email: str) -> Optional[str]:
    base = (email.split("@")[0] if "@" in email else "").strip().lower()
    base = "".join(ch for ch in base if ch.isalnum() or ch in {"_", "-"})[:32]
    # Handles must be ≥3 chars; skip auto-generation for very short email prefixes
    # so we don't persist an invalid handle that later fails the PATCH validator.
    if len(base) < 3:
        return None
    candidate = base
    suffix = 1
    while db.query(AppUser).filter(AppUser.handle == candidate).first() is not None:
        suffix += 1
        candidate = f"{base[:24]}-{suffix}"
    return candidate


def ensure_user_quota(db: Session, app_user_id: str) -> None:
    quota = db.query(UserQuota).filter(UserQuota.app_user_id == app_user_id).first()
    if quota is not None:
        return
    now = datetime.utcnow()
    db.add(
        UserQuota(
            app_user_id=app_user_id,
            plan="free",
            token_limit_daily=None,
            token_limit_monthly=None,
            credit_limit_monthly=None,
            tokens_used_monthly=0,
            credits_used_monthly=0.0,
            period_start=now,
            period_end=None,
            created_at=now,
            updated_at=now,
        )
    )


def upsert_app_user_from_supertokens(
    db: Session,
    *,
    st_user_id: str,
    email: str,
    name: Optional[str] = None,
    profile_picture: Optional[str] = None,
) -> AppUser:
    """
    Deterministic app-user bridge:
    1) by supertokens_user_id
    2) fallback by email (legacy user merge)
    """
    user = db.query(AppUser).filter(AppUser.supertokens_user_id == st_user_id).first()
    if user is None:
        user = db.query(AppUser).filter(AppUser.email == email).first()
        if user is not None:
            user.supertokens_user_id = st_user_id

    now = datetime.utcnow()
    if user is None:
        user = AppUser(
            supertokens_user_id=st_user_id,
            email=email,
            handle=generate_unique_handle(db, email),
            name=name,
            profile_picture=profile_picture,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        db.add(user)
        db.flush()
    else:
        user.email = email or user.email
        # Only seed name/picture from the OAuth provider when the user has not
        # already set their own value.  This lets users customise their profile
        # via Settings without having it overwritten on every Google login.
        if name and not user.name:
            user.name = name
        if profile_picture and not user.profile_picture:
            user.profile_picture = profile_picture
        user.last_login_at = now
        user.updated_at = now

    ensure_user_quota(db, user.id)
    return user


def update_preferred_model(
    db: Session, supertokens_user_id: str, model: Optional[str]
) -> Optional[AppUser]:
    """Set (or clear) the preferred LLM model for an app user."""
    user = db.query(AppUser).filter(AppUser.supertokens_user_id == supertokens_user_id).first()
    if user is None:
        return None
    user.preferred_model = model
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


def update_long_term_memory_enabled(
    db: Session, supertokens_user_id: str, enabled: bool
) -> Optional[AppUser]:
    """Enable or disable long-term memory (Hindsight) for an app user."""
    user = db.query(AppUser).filter(AppUser.supertokens_user_id == supertokens_user_id).first()
    if user is None:
        return None
    user.long_term_memory_enabled = enabled
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


def get_active_app_user_from_st_id(db: Session, st_user_id: str) -> Optional[AppUser]:
    user = db.query(AppUser).filter(AppUser.supertokens_user_id == st_user_id).first()
    if user is None or not user.is_active:
        return None
    return user

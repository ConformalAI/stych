"""
Development-only authentication bypass.
ONLY works when ENV != production.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import AppUser

logger = logging.getLogger(__name__)

IS_PRODUCTION = os.getenv("ENV", "development") == "production"
DEV_MODE_ENABLED = os.getenv("DEV_MODE", "false").lower() == "true"


def is_dev_mode_allowed() -> bool:
    """Check if dev mode is allowed (not production + explicitly enabled)."""
    return not IS_PRODUCTION and DEV_MODE_ENABLED


def get_or_create_dev_user(
    db: Session,
    email: str = "dev@stych.local",
    name: str = "Dev User",
) -> Optional[AppUser]:
    """
    Get or create a development test user.

    Args:
        db: Database session
        email: Test user email
        name: Test user name

    Returns:
        AppUser object or None if dev mode not allowed
    """
    if not is_dev_mode_allowed():
        logger.warning("Dev mode authentication attempted in production - blocked")
        return None

    user = db.query(AppUser).filter(AppUser.email == email).first()

    if not user:
        # Create dev user — supertokens_user_id uses a stable synthetic value so
        # the ST session created programmatically in auth_routes can map back.
        now = datetime.utcnow()
        user = AppUser(
            email=email,
            supertokens_user_id=f"stych_dev_{email}",
            name=name,
            profile_picture=None,
            handle=None,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"Created dev user: {email}")
    else:
        # Update last login
        user.last_login_at = datetime.utcnow()
        user.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
        logger.info(f"Dev user logged in: {email}")

    return user

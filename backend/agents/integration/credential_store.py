"""
Per-user credential store for MCP integrations.

Credentials are encrypted at rest with Fernet symmetric encryption.
The key is derived from the existing SECRET_KEY env var — no new secrets needed.

Functions
---------
get_credentials(db, app_user_id, server_id)  -> dict | None
set_credentials(db, app_user_id, server_id, creds)  -> None
delete_credentials(db, app_user_id, server_id)  -> bool
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from sqlalchemy.orm import Session

from database.models import UserIntegration
from helpers.secrets_crypto import get_fernet as _fernet

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_credentials(db: Session, app_user_id: str, server_id: str) -> Optional[Dict[str, str]]:
    """
    Return the decrypted credentials dict for (user, server), or None if not set.
    """
    row: Optional[UserIntegration] = (
        db.query(UserIntegration)
        .filter(
            UserIntegration.app_user_id == app_user_id,
            UserIntegration.server_id == server_id,
            UserIntegration.is_active.is_(True),
        )
        .first()
    )
    if row is None:
        return None  # No row → not connected (user never opted in)
    if not row.credentials_enc:
        return (
            {}
        )  # Row exists but no credentials → connected (opt-in integrations like TryOpenData)
    try:
        decrypted = _fernet().decrypt(row.credentials_enc.encode()).decode()
        return json.loads(decrypted)
    except Exception:
        logger.exception(
            "Failed to decrypt credentials for user=%s server=%s", app_user_id, server_id
        )
        return None


def set_credentials(db: Session, app_user_id: str, server_id: str, creds: Dict[str, str]) -> None:
    """
    Upsert encrypted credentials for (user, server).
    Pass an empty dict for integrations that require no credentials (opt-in only).
    """
    # Never persist app-level OAuth client secrets per user.
    if creds.get("auth_mode") == "oauth":
        creds = {k: v for k, v in creds.items() if k != "client_secret"}

    # Store NULL for empty-credential integrations (the row presence signals "connected")
    encrypted = _fernet().encrypt(json.dumps(creds).encode()).decode() if creds else None
    row: Optional[UserIntegration] = (
        db.query(UserIntegration)
        .filter(
            UserIntegration.app_user_id == app_user_id,
            UserIntegration.server_id == server_id,
        )
        .first()
    )
    if row is None:
        from datetime import datetime

        row = UserIntegration(
            app_user_id=app_user_id,
            server_id=server_id,
            credentials_enc=encrypted,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(row)
    else:
        from datetime import datetime

        row.credentials_enc = encrypted
        row.is_active = True
        row.updated_at = datetime.utcnow()
    db.commit()


def delete_credentials(db: Session, app_user_id: str, server_id: str) -> bool:
    """
    Remove credentials for (user, server). Returns True if a row was deleted.
    """
    row: Optional[UserIntegration] = (
        db.query(UserIntegration)
        .filter(
            UserIntegration.app_user_id == app_user_id,
            UserIntegration.server_id == server_id,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True

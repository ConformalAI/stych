"""Refresh expired Google OAuth tokens for MCP integrations."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Refresh access tokens this many milliseconds before expiry.
_REFRESH_MARGIN_MS = 5 * 60 * 1000


def maybe_refresh_oauth_credentials(
    db: Session,
    app_user_id: str,
    server_id: str,
    creds: Dict[str, Any],
) -> Dict[str, Any]:
    """
    If OAuth creds are expired (or near expiry), exchange the refresh_token
    for a new access_token and persist the update.
    """
    if creds.get("auth_mode") != "oauth":
        return creds

    expiry_ms = int(creds.get("expiry_date") or 0)
    now_ms = int(time.time() * 1000)
    if expiry_ms > now_ms + _REFRESH_MARGIN_MS:
        return creds

    refresh_token = creds.get("refresh_token") or ""
    if not refresh_token:
        logger.warning(
            "[oauth_refresh] token expired, no refresh_token user=%s server=%s",
            app_user_id,
            server_id,
        )
        return creds

    client_id = creds.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("[oauth_refresh] missing Google OAuth client config")
        return creds

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
    except Exception as exc:
        logger.warning("[oauth_refresh] request failed server=%s: %s", server_id, exc)
        return creds

    if resp.status_code != 200:
        logger.warning(
            "[oauth_refresh] failed server=%s status=%s body=%s",
            server_id,
            resp.status_code,
            resp.text[:300],
        )
        return creds

    token_data = resp.json()
    updated = dict(creds)
    updated["access_token"] = token_data.get("access_token", updated.get("access_token", ""))
    updated["expiry_date"] = int((time.time() + token_data.get("expires_in", 3600)) * 1000)
    if token_data.get("refresh_token"):
        updated["refresh_token"] = token_data["refresh_token"]
    if token_data.get("scope"):
        updated["scope"] = token_data["scope"]

    from agents.integration.credential_store import set_credentials

    set_credentials(db, app_user_id, server_id, updated)
    logger.info("[oauth_refresh] refreshed access token user=%s server=%s", app_user_id, server_id)
    return updated

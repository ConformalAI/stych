"""Startup security checks for production deployments."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSECURE_SECRET_KEYS = frozenset(
    {
        "",
        "your_secret_key_here_please_change_in_production",
        "default-secret-key-change-me",
        "change-me",
    }
)


def validate_security_config() -> None:
    """
    Warn or fail when obvious insecure defaults are in use.

    In production (ENV != development and DEV_MODE not true), a weak SECRET_KEY
    raises RuntimeError so the process does not start with decryptable integration
    credentials.
    """
    secret_key = os.environ.get("SECRET_KEY", "")
    dev_mode = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")
    env = os.environ.get("ENV", "development").lower()

    if secret_key in _INSECURE_SECRET_KEYS:
        msg = (
            "SECRET_KEY is unset or still using the default placeholder. "
            "Set a long random value in backend/.env before storing integration credentials."
        )
        if env == "production" or (env != "development" and not dev_mode):
            raise RuntimeError(msg)
        logger.warning("[security] %s", msg)

    if not os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") and os.environ.get(
        "GOOGLE_OAUTH_CLIENT_ID"
    ):
        logger.warning(
            "[security] GOOGLE_OAUTH_CLIENT_ID is set but GOOGLE_OAUTH_CLIENT_SECRET is missing — "
            "Google integration OAuth will fail at token exchange."
        )

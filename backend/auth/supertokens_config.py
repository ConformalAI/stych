"""
SuperTokens initialization and user-sync configuration.

This module:
  - Initialises the SuperTokens SDK (must be called before the FastAPI app is created)
  - Configures recipes: EmailPassword, ThirdParty (Google), Session, Dashboard
  - Overrides sign-up/sign-in callbacks to sync SuperTokens users into app-owned
    `app_users` table (bridge key: supertokens_user_id)

Flow:
  1. User signs up / signs in via SuperTokens (email+password or Google OAuth)
  2. ST calls our override callback after successful auth
  3. We upsert the user into `app_users` keyed by supertokens_user_id
  4. All subsequent requests use SuperTokens session cookies; `get_current_user`
     dependency resolves app user by supertokens_user_id
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local user sync helper
# ---------------------------------------------------------------------------


def _sync_local_user(
    st_user_id: str,
    email: str,
    name: Optional[str] = None,
    picture: Optional[str] = None,
) -> None:
    """
    Upsert a SuperTokens user into Stych's app_users table.

    Uses its own short-lived DB session so it can be called from override
    callbacks that run outside the normal FastAPI request context.
    """
    from database.app_user_store import upsert_app_user_from_supertokens
    from database.connection import SessionLocal

    db = SessionLocal()
    try:
        user = upsert_app_user_from_supertokens(
            db,
            st_user_id=st_user_id,
            email=email,
            name=name,
            profile_picture=picture,
        )
        db.commit()
        logger.info(
            "[ST] Synced app user: app_user_id=%s email=%s st_id=%s",
            user.id,
            email,
            st_user_id[:8],
        )
    except Exception as exc:
        db.rollback()
        logger.error("[ST] Failed to sync app user st_id=%s: %s", st_user_id, exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Recipe API overrides
# ---------------------------------------------------------------------------


def _override_emailpassword_apis(original_impl):
    """Hook into EmailPassword sign-up to sync user into local DB."""
    original_sign_up_post = original_impl.sign_up_post

    async def sign_up_post(*args, **kwargs):
        result = await original_sign_up_post(*args, **kwargs)
        if result.status == "OK":
            st_user_id = result.user.id
            email = result.user.emails[0] if result.user.emails else ""
            _sync_local_user(st_user_id, email)
        return result

    original_impl.sign_up_post = sign_up_post
    return original_impl


def _override_thirdparty_apis(original_impl):
    """Hook into ThirdParty (Google etc.) sign-in/up to sync user into local DB."""
    original_sign_in_up_post = original_impl.sign_in_up_post

    async def sign_in_up_post(*args, **kwargs):
        result = await original_sign_in_up_post(*args, **kwargs)
        if result.status == "OK":
            st_user_id = result.user.id
            email = result.user.emails[0] if result.user.emails else ""

            # Extract name / picture from the raw provider payload (Google OIDC)
            name: Optional[str] = None
            picture: Optional[str] = None
            raw = getattr(result, "raw_user_info_from_provider", None)
            if raw:
                id_token_payload = getattr(raw, "from_id_token_payload", None) or {}
                name = id_token_payload.get("name")
                picture = id_token_payload.get("picture")

            _sync_local_user(st_user_id, email, name=name, picture=picture)
        return result

    original_impl.sign_in_up_post = sign_in_up_post
    return original_impl


# ---------------------------------------------------------------------------
# Main init function
# ---------------------------------------------------------------------------


def init_supertokens(config: Dict[str, Any]) -> None:
    """
    Initialise the SuperTokens SDK from the merged app config dict.

    Must be called once at module level in main.py BEFORE the FastAPI app
    object is created so the SDK can register its internal route handlers.
    """
    try:
        from supertokens_python import InputAppInfo, SupertokensConfig, init
        from supertokens_python.recipe import (
            dashboard,
            emailpassword,
            session,
            thirdparty,
        )
    except ImportError:
        logger.error(
            "supertokens-python is not installed. "
            "Add 'supertokens-python[fastapi]' to pyproject.toml and reinstall."
        )
        raise

    st_cfg = config.get("supertokens", {})

    connection_uri = st_cfg.get(
        "connection_uri",
        os.getenv("SUPERTOKENS_CONNECTION_URI", "http://localhost:3567"),
    )
    api_key = st_cfg.get("api_key", os.getenv("SUPERTOKENS_API_KEY", ""))
    app_name = st_cfg.get("app_name", "Stych")
    api_domain = st_cfg.get("api_domain", os.getenv("API_DOMAIN", "http://localhost:8000"))
    website_domain = st_cfg.get(
        "website_domain", os.getenv("WEBSITE_DOMAIN", "http://localhost:5173")
    )
    api_base_path = st_cfg.get("api_base_path", "/auth")
    website_base_path = st_cfg.get("website_base_path", "/auth")

    google_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    google_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

    # Build recipe list
    recipe_list = [
        session.init(
            # Use header-based token transfer so sessions work correctly through
            # HTTP reverse proxies (Vite dev / nginx) and across origins (tunnel,
            # localhost, production) without cookie Secure/SameSite restrictions.
            get_token_transfer_method=lambda *_: "header",
        ),
        emailpassword.init(
            override=emailpassword.InputOverrideConfig(
                apis=_override_emailpassword_apis,
            )
        ),
        dashboard.init(),
    ]

    # Only add Google OAuth provider if credentials are configured
    if google_client_id and google_client_secret:
        recipe_list.append(
            thirdparty.init(
                sign_in_and_up_feature=thirdparty.SignInAndUpFeature(
                    providers=[
                        thirdparty.ProviderInput(
                            config=thirdparty.ProviderConfig(
                                third_party_id="google",
                                clients=[
                                    thirdparty.ProviderClientConfig(
                                        client_id=google_client_id,
                                        client_secret=google_client_secret,
                                    )
                                ],
                            )
                        )
                    ]
                ),
                override=thirdparty.InputOverrideConfig(
                    apis=_override_thirdparty_apis,
                ),
            )
        )
        logger.info("[ST] Google OAuth provider configured")
    else:
        logger.warning(
            "[ST] GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not set — "
            "Google sign-in will be unavailable"
        )

    init(
        app_info=InputAppInfo(
            app_name=app_name,
            api_domain=api_domain,
            website_domain=website_domain,
            api_base_path=api_base_path,
            website_base_path=website_base_path,
        ),
        supertokens_config=SupertokensConfig(
            connection_uri=connection_uri,
            api_key=api_key if api_key else None,
        ),
        framework="fastapi",
        recipe_list=recipe_list,
        mode="asgi",
    )

    logger.info(
        "[ST] SuperTokens initialised: core=%s app=%s api_domain=%s website_domain=%s",
        connection_uri,
        app_name,
        api_domain,
        website_domain,
    )

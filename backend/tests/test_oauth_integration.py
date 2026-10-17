"""Tests for MCP integration OAuth helpers."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from agents.integration.credential_store import set_credentials
from database.models import AppUser, OAuthPendingState
from helpers.oauth_state_store import consume_oauth_state, create_oauth_state


def _ensure_app_user(db_session, user_id: str = "user-1") -> str:
    row = db_session.get(AppUser, user_id)
    if row is None:
        row = AppUser(
            id=user_id,
            supertokens_user_id=f"st-{user_id}",
            email=f"{user_id}@example.com",
        )
        db_session.add(row)
        db_session.commit()
    return user_id


class TestOAuthStateStore:
    def test_create_and_consume(self, db_session):
        user_id = _ensure_app_user(db_session)
        state = create_oauth_state(db_session, user_id, "google_sheets", ttl_seconds=600)
        assert state

        data = consume_oauth_state(db_session, state)
        assert data == {"user_id": user_id, "server_id": "google_sheets"}

        # One-time use
        assert consume_oauth_state(db_session, state) is None

    def test_expired_state_returns_none(self, db_session):
        user_id = _ensure_app_user(db_session)
        state = "expired-test-state"
        db_session.add(
            OAuthPendingState(
                state=state,
                app_user_id=user_id,
                server_id="google_drive",
                expires_at=datetime.utcnow() - timedelta(seconds=30),
                created_at=datetime.utcnow(),
            )
        )
        db_session.commit()

        assert consume_oauth_state(db_session, state) is None


class TestOAuthCredentialPersistence:
    def test_set_credentials_strips_client_secret_for_oauth(self, db_session):
        user_id = _ensure_app_user(db_session)
        set_credentials(
            db_session,
            user_id,
            "google_sheets",
            {
                "auth_mode": "oauth",
                "access_token": "at",
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "should-not-persist",
            },
        )

        from agents.integration.credential_store import get_credentials

        creds = get_credentials(db_session, user_id, "google_sheets")
        assert creds is not None
        assert creds.get("client_secret") is None
        assert creds.get("refresh_token") == "rt"


class TestOAuthTokenRefresh:
    def test_skips_when_token_still_valid(self, db_session):
        from helpers.oauth_token_refresh import maybe_refresh_oauth_credentials

        creds = {
            "auth_mode": "oauth",
            "access_token": "old",
            "refresh_token": "rt",
            "expiry_date": int((time.time() + 3600) * 1000),
        }
        out = maybe_refresh_oauth_credentials(db_session, "u1", "google_sheets", creds)
        assert out["access_token"] == "old"

    @patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_SECRET": "test-secret"})
    @patch("helpers.oauth_token_refresh.httpx.Client")
    def test_refreshes_expired_token(self, mock_client_cls, db_session):
        from helpers.oauth_token_refresh import maybe_refresh_oauth_credentials

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-access",
            "expires_in": 3600,
            "scope": "email openid",
        }
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        creds = {
            "auth_mode": "oauth",
            "access_token": "old",
            "refresh_token": "rt",
            "expiry_date": int((time.time() - 60) * 1000),
            "client_id": "cid",
        }
        out = maybe_refresh_oauth_credentials(db_session, "u1", "google_sheets", creds)
        assert out["access_token"] == "new-access"
        assert out["scope"] == "email openid"

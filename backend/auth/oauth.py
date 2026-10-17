"""
Google OAuth token verification.
Verifies Google ID tokens received from the frontend after Google Sign-In.
"""

import logging
import os
from typing import Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)


def verify_google_id_token(token: str) -> Optional[dict]:
    """
    Verify a Google ID token and extract user information.

    The frontend sends a Google ID token (JWT credential) after the user
    signs in with Google. This function verifies the token with Google's
    servers and extracts the user profile data.

    Args:
        token: Google ID token (JWT credential from Google Sign-In)

    Returns:
        Dictionary with user info if valid:
            - google_id: Google's unique user ID (sub claim)
            - email: User's email address
            - name: User's display name
            - picture: URL to user's profile picture
            - email_verified: Whether email is verified by Google
        None if token is invalid
    """
    google_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    if not google_client_id:
        logger.error("GOOGLE_OAUTH_CLIENT_ID is not set")
        return None

    try:
        idinfo = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            google_client_id,
        )

        # Verify the issuer
        if idinfo["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
            logger.error(f"Invalid token issuer: {idinfo['iss']}")
            return None

        return {
            "google_id": idinfo["sub"],
            "email": idinfo["email"],
            "name": idinfo.get("name"),
            "picture": idinfo.get("picture"),
            "email_verified": idinfo.get("email_verified", False),
        }

    except ValueError as e:
        logger.error(f"Google token verification failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error during Google token verification: {e}")
        return None

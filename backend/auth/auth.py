"""
Authentication and authorization module.
JWT-based authentication with Google OAuth.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class TokenData(BaseModel):
    """JWT token data extracted from a verified token."""

    user_id: str
    email: str
    name: Optional[str] = None
    exp: datetime
    session_id: Optional[str] = None


def create_access_token(
    data: dict, secret_key: str, expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create JWT access token.

    Args:
        data: Token payload (should include user_id, email, name, session_id)
        secret_key: Secret key for signing
        expires_delta: Token expiration time

    Returns:
        Encoded JWT token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=30)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, secret_key, algorithm="HS256")

    return encoded_jwt


def verify_token(token: str, secret_key: str) -> Optional[TokenData]:
    """
    Verify JWT token and return its data.

    Args:
        token: JWT token string
        secret_key: Secret key for verification

    Returns:
        TokenData (including session_id) if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, secret_key, algorithms=["HS256"])
        user_id: str = payload.get("user_id")
        email: str = payload.get("email")
        name: str = payload.get("name")
        session_id: str = payload.get("session_id")
        exp = datetime.fromtimestamp(payload.get("exp"))

        if user_id is None or email is None:
            return None

        return TokenData(
            user_id=user_id,
            email=email,
            name=name,
            exp=exp,
            session_id=session_id,
        )

    except jwt.PyJWTError as e:
        logger.error(f"Token verification failed: {e}")
        return None

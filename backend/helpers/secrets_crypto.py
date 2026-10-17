"""
Symmetric encryption for sensitive values persisted at rest.

Used for datasource connection URLs in session snapshots (so a restored SQL
session can resume without re-entering credentials) and as the single source of
the Fernet key for the MCP credential store.

The key is derived from the existing ``SECRET_KEY`` env var — no new secret is
introduced. ``SECRET_KEY`` must be stable across restarts and identical across
replicas, or previously-encrypted values will not decrypt (same assumption the
credential store already relies on).

Encrypted strings are tagged with a version prefix so readers can distinguish
ciphertext from legacy/plaintext values.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Tag prepended to every ciphertext so callers can recognise an encrypted value.
ENC_PREFIX = "enc:v1:"


def fernet_key() -> bytes:
    """Derive a 32-byte urlsafe-base64 Fernet key from the app SECRET_KEY."""
    secret = os.environ.get("SECRET_KEY", "default-secret-key-change-me")
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def get_fernet():
    """Return a Fernet instance (lazy import so cryptography stays optional at import time)."""
    from cryptography.fernet import Fernet  # noqa: PLC0415

    return Fernet(fernet_key())


def is_encrypted(value: object) -> bool:
    """True if *value* is a string produced by ``encrypt_value``."""
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypt_value(plaintext: str) -> Optional[str]:
    """
    Return an ``ENC_PREFIX``-tagged ciphertext for *plaintext*, or None if
    encryption is unavailable (cryptography missing / key error). Callers should
    fall back to redaction on None so a plaintext secret is never persisted.
    """
    if plaintext is None:
        return None
    try:
        token = get_fernet().encrypt(plaintext.encode()).decode()
        return f"{ENC_PREFIX}{token}"
    except Exception as exc:  # noqa: BLE001 — never let crypto issues persist plaintext
        logger.warning("encrypt_value failed (%s); caller should redact instead", exc)
        return None


def decrypt_value(value: str) -> Optional[str]:
    """
    Decrypt an ``ENC_PREFIX``-tagged token back to plaintext. Returns None if
    *value* is not encrypted or cannot be decrypted (wrong key, corrupt, etc.).
    """
    if not is_encrypted(value):
        return None
    token = value[len(ENC_PREFIX) :]
    try:
        return get_fernet().decrypt(token.encode()).decode()
    except Exception as exc:  # noqa: BLE001
        logger.warning("decrypt_value failed: %s", exc)
        return None

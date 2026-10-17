"""Shared pytest configuration for backend tests."""

from __future__ import annotations

import os

import pytest

# Unit tests must not inherit a developer shell's APP_DATABASE_URL (the live
# app DB). Integration tests that need Postgres live under tests/integration/
# and load their own env.
os.environ["APP_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_DB_REQUIRE_POSTGRES"] = "false"
os.environ["APP_DB_SCHEMA"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")


@pytest.fixture()
def db_session():
    from database.connection import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

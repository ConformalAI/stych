"""
Regression tests for snapshot credential handling.

Pins the bug where a restored SQL session carried connection_url="[REDACTED]"
(a truthy but unparseable string), which routed analysis to the SQL path and
crashed create_engine with "Could not parse SQLAlchemy URL". Connection URLs are
now encrypted at rest so a restored session resumes; legacy/undecryptable values
are dropped (and the runner rejects any placeholder that slips through).
"""

import os

import pandas as pd
import pytest

# Stable key so encrypt/decrypt round-trips within the test process.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-snapshot-crypto")

from agents.sql_agent.runner import PostgresSQLRunner, SQLRunnerError  # noqa: E402
from agents.state import (  # noqa: E402
    _encrypt_sensitive_meta,
    _restore_sensitive_meta,
)
from helpers.secrets_crypto import (  # noqa: E402
    ENC_PREFIX,
    decrypt_value,
    encrypt_value,
    is_encrypted,
)

_URL = "postgresql+psycopg2://user:p%40ss@host.example:5433/db"


def test_encrypt_decrypt_round_trip():
    token = encrypt_value(_URL)
    assert token is not None
    assert token.startswith(ENC_PREFIX)
    assert _URL not in token  # ciphertext does not leak the plaintext
    assert is_encrypted(token)
    assert decrypt_value(token) == _URL


def test_decrypt_rejects_non_encrypted():
    assert decrypt_value("[REDACTED]") is None
    assert decrypt_value(_URL) is None  # plaintext, not our token
    assert not is_encrypted("[REDACTED]")


def test_snapshot_meta_encrypts_connection_url():
    meta = {"source": "postgres", "connection_url": _URL, "schema": "public", "table": "t"}
    safe = _encrypt_sensitive_meta(meta)
    # Sensitive value is encrypted, non-sensitive values pass through untouched.
    assert is_encrypted(safe["connection_url"])
    assert _URL not in safe["connection_url"]
    assert safe["source"] == "postgres"
    assert safe["schema"] == "public"


def test_round_trip_restores_usable_connection_url():
    meta = {
        "source": "postgres",
        "connection_url": _URL,
        "temp_schema": "stych_temp",
        "temp_table": "stych_abc123",
    }
    restored = _restore_sensitive_meta(_encrypt_sensitive_meta(meta))
    # URL comes back decrypted and usable...
    assert restored["connection_url"] == _URL
    # ...and the working-copy refs SURVIVE the restore: the copy is a real table
    # in the user's database, and SQLAgent verifies it on first touch instead of
    # wiping the reference (which silently threw away the user's changes).
    assert restored["temp_schema"] == "stych_temp"
    assert restored["temp_table"] == "stych_abc123"


def test_structured_warehouse_credentials_are_encrypted_as_one_payload():
    payload = '{"account":"org-account","private_key":"-----BEGIN PRIVATE KEY-----\\nvery-secret\\n-----END PRIVATE KEY-----","user":"analyst"}'
    safe = _encrypt_sensitive_meta(
        {"source": "snowflake", "connection_config": payload, "schema": "PUBLIC"}
    )
    assert is_encrypted(safe["connection_config"])
    assert "very-secret" not in safe["connection_config"]
    restored = _restore_sensitive_meta(safe)
    assert restored["connection_config"] == payload


def test_bigquery_and_databricks_secret_fields_are_encrypted():
    payload = (
        '{"access_token":"dapi-secret","service_account_json":'
        '{"type":"service_account","private_key":"bq-secret"}}'
    )
    safe = _encrypt_sensitive_meta(
        {
            "source": "databricks",
            "connection_config": payload,
            "access_token": "dapi-secret",
            "service_account_json": '{"private_key":"bq-secret"}',
        }
    )
    assert is_encrypted(safe["connection_config"])
    assert is_encrypted(safe["access_token"])
    assert is_encrypted(safe["service_account_json"])
    assert "dapi-secret" not in safe["connection_config"]
    assert "bq-secret" not in safe["service_account_json"]
    restored = _restore_sensitive_meta(safe)
    assert restored["connection_config"] == payload
    assert restored["access_token"] == "dapi-secret"


def test_restore_drops_legacy_redacted_url():
    # A snapshot written before encryption stored the placeholder.
    legacy = {"source": "postgres", "connection_url": "[REDACTED]"}
    restored = _restore_sensitive_meta(legacy)
    # Dropped entirely so is_sql_datasource() is False and create_engine is never called.
    assert "connection_url" not in restored
    assert restored["source"] == "postgres"


def test_restore_drops_undecryptable_url():
    bad = {"source": "postgres", "connection_url": ENC_PREFIX + "not-a-valid-token"}
    restored = _restore_sensitive_meta(bad)
    assert "connection_url" not in restored


def test_runner_rejects_placeholder_url():
    with pytest.raises(SQLRunnerError):
        PostgresSQLRunner("[REDACTED]")
    with pytest.raises(SQLRunnerError):
        PostgresSQLRunner("not-a-url")
    # A well-formed URL is accepted (no connection attempt in __init__).
    assert PostgresSQLRunner(_URL).connection_url == _URL


class _FakeStorage:
    """In-memory snapshot store: {(session_id, name): bytes}."""

    def __init__(self):
        self.objects = {}

    def save_snapshot_object(self, session_id, name, content):
        self.objects[(session_id, name)] = content

    def load_snapshot_object(self, session_id, name):
        return self.objects.get((session_id, name))

    def delete_snapshot_object(self, session_id, name):
        return self.objects.pop((session_id, name), None) is not None

    def delete_session_snapshots(self, session_id):
        keys = [k for k in self.objects if k[0] == session_id]
        for k in keys:
            del self.objects[k]
        return len(keys)


def test_end_to_end_sql_session_resumes_after_snapshot(monkeypatch):
    """The real bug scenario: a SQL session snapshotted on one worker and restored
    on another (fresh) worker must come back as a usable SQL datasource."""
    from datetime import datetime

    import pandas as pd

    import helpers.storage as storage_mod
    from agents.state import AgentState, DataContext, StateManager

    fake = _FakeStorage()
    monkeypatch.setattr(storage_mod, "storage_manager", fake)

    preview = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    dc = DataContext(
        file_id="postgres:public.t:abc",
        filename="PostgreSQL: public.t",
        raw_dataframe=preview.copy(deep=True),
        working_dataframe=preview.copy(deep=True),
        uploaded_at=datetime.now(),
        metadata={
            "user_id": "u1",
            "source": "postgres",
            "connection_url": _URL,
            "schema": "public",
            "table": "t",
            "temp_schema": "stych_temp",
            "temp_table": "stych_stale",
        },
    )
    src = AgentState(session_id="sess-1")
    src.metadata["user_id"] = "u1"
    src.set_data_context(dc)

    sm = StateManager()
    sm._snapshot_data_sync(src)
    assert ("sess-1", "postgres:public.t:abc.parquet") not in fake.objects
    assert ("sess-1", "postgres:public.t:abc.raw.parquet") not in fake.objects
    assert src.data_context.working_dataframe.empty
    assert src.data_context.metadata["storage_mode"] == "remote_reference"

    # Fresh worker: no in-memory session, restore from snapshot.
    dst = AgentState(session_id="sess-1")
    dst.metadata["user_id"] = "u1"
    sm._restore_snapshot_sync(dst)

    assert dst.data_context is not None
    # Connection URL is recovered (decrypted) → usable SQL datasource, no crash.
    assert dst.data_context.metadata["connection_url"] == _URL
    assert dst.data_context.is_sql_datasource() is True
    # Working-copy refs survive the restore (SQLAgent verifies on first touch);
    # the user's temp-table modifications are not thrown away by a restart.
    assert dst.data_context.metadata.get("temp_table") == src.data_context.metadata.get(
        "temp_table"
    )
    # And the on-disk snapshot never held the plaintext URL.
    meta_blob = fake.objects[("sess-1", "postgres:public.t:abc.meta.json")].decode()
    assert _URL not in meta_blob
    assert ENC_PREFIX in meta_blob


def test_remote_source_snapshot_persists_metadata_without_rows(monkeypatch):
    from datetime import datetime

    import helpers.storage as storage_mod
    from agents.state import AgentState, DataContext, StateManager

    fake = _FakeStorage()
    monkeypatch.setattr(storage_mod, "storage_manager", fake)
    empty = pd.DataFrame(columns=["id", "amount"])
    context = DataContext(
        file_id="snowflake:PUBLIC.orders:abc",
        filename="Snowflake: PUBLIC.orders",
        raw_dataframe=empty,
        working_dataframe=empty,
        uploaded_at=datetime.now(),
        metadata={
            "source": "snowflake",
            "storage_mode": "remote_reference",
            "connection_config": '{"account":"org"}',
            "source_columns": [
                {"name": "id", "db_type": "NUMBER", "kind": "number"},
                {"name": "amount", "db_type": "NUMBER", "kind": "number"},
            ],
        },
    )
    state = AgentState(session_id="remote-session")
    state.set_data_context(context)
    manager = StateManager()
    manager._snapshot_data_sync(state)

    assert ("remote-session", "snowflake:PUBLIC.orders:abc.parquet") not in fake.objects
    assert ("remote-session", "snowflake:PUBLIC.orders:abc.raw.parquet") not in fake.objects

    restored = AgentState(session_id="remote-session")
    manager._restore_snapshot_sync(restored)
    assert restored.data_context is not None
    assert restored.data_context.raw_dataframe is restored.data_context.working_dataframe
    assert restored.data_context.working_dataframe.empty
    assert list(restored.data_context.working_dataframe.columns) == ["id", "amount"]

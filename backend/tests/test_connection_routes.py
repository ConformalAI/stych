"""User-level saved datasource connections."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.dependencies import get_config, get_current_user
from database.connection import Base, get_db
from database.connection_store import (
    create_connection,
    decrypt_credentials,
    get_connection,
    list_connections,
    merge_connection_update,
    pin_table,
    public_hint,
)
from datasources.adapters import AdapterColumn

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_store_encrypts_credentials_and_isolates_users(db_session):
    row = create_connection(
        db_session,
        app_user_id="user-1",
        connector_id="databricks",
        name="Prod",
        credentials={
            "access_token": "dapi-secret",
            "catalog": "main",
            "server_hostname": "adb.example",
        },
        hint=public_hint(
            {"access_token": "dapi-secret", "catalog": "main", "server_hostname": "adb.example"}
        ),
    )
    assert "dapi-secret" not in (row.credentials_enc or "")
    assert decrypt_credentials(row.credentials_enc)["access_token"] == "dapi-secret"
    assert json.loads(row.config_json)["catalog"] == "main"
    assert "access_token" not in json.loads(row.config_json)
    assert get_connection(db_session, "user-2", row.id) is None
    assert list_connections(db_session, "user-2") == []
    assert list_connections(db_session, "user-1")[0].id == row.id


def test_public_hint_strips_url_password():
    hint = public_hint({"connection_url": "postgresql://alice:s3cret@db.example:5432/analytics"})
    assert hint["host"] == "db.example"
    assert hint["database"] == "analytics"
    assert hint["user"] == "alice"
    assert "s3cret" not in json.dumps(hint)


def test_public_hint_url_endpoint_wins_over_component_defaults():
    hint = public_hint(
        {
            "connection_url": "mssql+pymssql://sa:secret@127.0.0.1:14330/master",
            "port": 1433,
            "database": "master",
        }
    )

    assert hint["host"] == "127.0.0.1"
    assert hint["port"] == 14330
    assert hint["database"] == "master"
    assert "secret" not in json.dumps(hint)


def test_successful_retest_repairs_stale_endpoint_hint(db_session, monkeypatch):
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "sqlserver",
            "connection": {"connection_url": "mssql+pymssql://sa:secret@127.0.0.1:14330/master"},
        },
    ).json()
    row = get_connection(db_session, "user-1", created["id"])
    row.config_json = json.dumps({"host": "127.0.0.1", "port": 1433, "database": "master"})
    db_session.commit()

    tested = client.post(f"/api/connections/{row.id}/test", json={})

    assert tested.status_code == 200
    fetched = client.get(f"/api/connections/{row.id}").json()
    assert fetched["hint"]["port"] == 14330
    assert "secret" not in json.dumps(fetched)


def test_merge_connection_update_keeps_blank_secrets():
    merged = merge_connection_update(
        {"private_key": "SECRET", "account": "old", "user": "analyst"},
        {"private_key": "", "account": "new-acct"},
    )
    assert merged["private_key"] == "SECRET"
    assert merged["account"] == "new-acct"
    assert merged["user"] == "analyst"


def test_patch_rotates_non_secrets_without_resending_key(db_session, monkeypatch):
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "snowflake",
            "name": "Analytics",
            "connection": {
                "account": "org-account",
                "user": "analyst",
                "private_key": _PEM,
                "warehouse": "COMPUTE_WH",
                "database": "DB",
            },
        },
    )
    cid = created.json()["id"]
    tested = client.post(f"/api/connections/{cid}/test", json={"connection": {"account": ""}})
    assert tested.status_code == 200, tested.text
    patched = client.patch(
        f"/api/connections/{cid}",
        json={
            "name": "Analytics West",
            "connection": {
                "account": "org-west",
                "user": "analyst",
                "private_key": "",
                "warehouse": "COMPUTE_WH",
                "database": "DB",
            },
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Analytics West"
    assert patched.json()["hint"]["account"] == "org-west"
    assert _PEM not in json.dumps(patched.json())
    stored = decrypt_credentials(get_connection(db_session, "user-1", cid).credentials_enc)
    assert stored["private_key"] == _PEM
    assert stored["account"] == "org-west"


def _client(db_session, monkeypatch, user: str = "user-1") -> TestClient:
    from api.connection_routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_config] = lambda: {
        "datasources": {
            "postgres": {"enabled": True, "default_connection_url": "postgresql://localhost/app"},
            "snowflake": {"enabled": True},
            "databricks": {"enabled": True},
        }
    }

    async def fake_test(connector_id, params, **_kwargs):
        if connector_id == "snowflake":
            return dict(params)
        if connector_id == "databricks":
            return {**params, "scratch_catalog": "main", "scratch_schema": "stych_temp"}
        if connector_id == "postgres":
            return params or {"use_default": True}
        return dict(params)

    monkeypatch.setattr("api.connection_routes.test_and_normalize", fake_test)
    return TestClient(app)


def test_create_list_delete_does_not_leak_secrets(db_session, monkeypatch):
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "snowflake",
            "name": "Analytics",
            "connection": {
                "account": "org-account",
                "user": "analyst",
                "private_key": _PEM,
                "warehouse": "COMPUTE_WH",
                "database": "DB",
            },
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "Analytics"
    assert body["hint"]["account"] == "org-account"
    assert "private_key" not in json.dumps(body)
    assert _PEM not in json.dumps(body)

    listed = client.get("/api/connections", params={"connector_id": "snowflake"})
    assert [c["id"] for c in listed.json()["connections"]] == [body["id"]]
    assert (
        client.get("/api/connections", params={"connector_id": "postgres"}).json()["connections"]
        == []
    )

    other = _client(db_session, monkeypatch, user="user-2")
    assert other.get(f"/api/connections/{body['id']}").status_code == 404
    assert other.delete(f"/api/connections/{body['id']}").status_code == 404

    assert client.delete(f"/api/connections/{body['id']}").status_code == 200
    assert client.get(f"/api/connections/{body['id']}").status_code == 404


def test_pin_unpin_and_attach_stamps_connection_id(db_session, monkeypatch):

    class _Session:
        def __init__(self):
            self.contexts = []

        def add_data_context(self, context):
            self.contexts.append(context)

        def get_all_data_contexts(self):
            return list(self.contexts)

        @property
        def dataset_count(self):
            return len(self.contexts)

    class _State:
        def __init__(self):
            self.session = _Session()

        def get_or_create_session(self, session_id, app_user_id=None):
            return self.session

        def mark_dirty(self, session_id):
            return None

        async def snapshot_data_async(self, session):
            return None

    manager = _State()
    monkeypatch.setattr("api.remote_source.state_manager", manager)
    monkeypatch.setattr("api.remote_source.get_langfuse_tracker", lambda: None)
    monkeypatch.setattr(
        "api.remote_source._probe_source",
        lambda context: ([AdapterColumn("id", "NUMBER", "number")], 1),
    )

    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "snowflake",
            "connection": {
                "account": "org-account",
                "user": "analyst",
                "private_key": _PEM,
                "warehouse": "COMPUTE_WH",
                "database": "DB",
            },
        },
    )
    cid = created.json()["id"]
    pinned = client.post(
        f"/api/connections/{cid}/tables",
        json={"schema_name": "PUBLIC", "table_name": "T"},
    )
    assert pinned.status_code == 200, pinned.text
    tid = pinned.json()["id"]
    assert (
        client.post(
            f"/api/connections/{cid}/tables",
            json={"schema_name": "PUBLIC", "table_name": "T"},
        ).json()["id"]
        == tid
    )

    attached = client.post(
        f"/api/connections/{cid}/tables/{tid}/attach",
        json={"session_id": "session-1"},
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["source_type"] == "snowflake"
    context = manager.session.contexts[0]
    assert context.metadata["connection_id"] == cid
    stored = json.loads(context.metadata["connection_config"])
    assert stored["private_key"] == _PEM
    assert _PEM not in attached.json()["message"]

    direct = client.post(
        f"/api/connections/{cid}/attach",
        json={"session_id": "session-1", "schema_name": "PUBLIC", "table_name": "ORDERS"},
    )
    assert direct.status_code == 200, direct.text
    assert manager.session.contexts[1].metadata["connection_id"] == cid
    again = client.post(
        f"/api/connections/{cid}/tables/{tid}/attach",
        json={"session_id": "session-1"},
    )
    assert again.status_code == 409, again.text
    assert "already in this chat" in again.json()["detail"]
    assert (
        client.post(f"/api/connections/{cid}/attach", json={"session_id": "session-1"}).status_code
        == 400
    )

    assert client.delete(f"/api/connections/{cid}/tables/{tid}").status_code == 200
    db_session.expire_all()
    assert get_connection(db_session, "user-1", cid) is not None
    assert get_connection(db_session, "user-1", cid).tables == []
    assert manager.session.contexts[0].metadata["connection_id"] == cid
    assert manager.session.contexts[1].metadata["connection_id"] == cid


def test_remote_tables_use_stored_credentials(db_session, monkeypatch):
    captured = {}

    def fake_list(config, database=None, schema="PUBLIC"):
        captured["config"] = config
        captured["schema"] = schema
        return [{"schema": schema, "table": "ORDERS"}]

    monkeypatch.setattr("api.connection_dispatch.list_snowflake_tables", fake_list)
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "snowflake",
            "connection": {
                "account": "org-account",
                "user": "analyst",
                "private_key": _PEM,
                "warehouse": "COMPUTE_WH",
                "database": "DB",
            },
        },
    )
    cid = created.json()["id"]
    listed = client.get(f"/api/connections/{cid}/remote-tables", params={"schema": "PUBLIC"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["tables"][0]["table"] == "ORDERS"
    assert captured["config"]["private_key"] == _PEM
    assert captured["schema"] == "PUBLIC"


def test_mongo_remote_tables_pass_database_once(db_session, monkeypatch):
    captured = {}

    def fake_list(*, database, connection_url=None, **kwargs):
        captured.update(database=database, connection_url=connection_url, kwargs=kwargs)
        return [{"collection": "dirty_cafe_sales"}]

    monkeypatch.setattr("api.connection_dispatch.list_mongodb_collections", fake_list)
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "mongodb",
            "connection": {
                "connection_url": "mongodb://127.0.0.1:27017",
                "database": "cafe_sales",
            },
        },
    )
    cid = created.json()["id"]
    listed = client.get(f"/api/connections/{cid}/remote-tables", params={"schema": "cafe_sales"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["collections"] == ["dirty_cafe_sales"]
    assert captured == {
        "database": "cafe_sales",
        "connection_url": "mongodb://127.0.0.1:27017",
        "kwargs": {"host": None, "port": 27017, "user": "", "password": ""},
    }


def test_mongo_namespace_discovery_uses_saved_database(db_session, monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return ["admin", "cafe_sales", "reporting"]

    monkeypatch.setattr("api.connection_dispatch.list_mongodb_databases", fake_list)
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "mongodb",
            "connection": {
                "connection_url": "mongodb://127.0.0.1:27017",
                "database": "cafe_sales",
            },
        },
    )
    cid = created.json()["id"]

    discovered = client.get(f"/api/connections/{cid}/namespaces")

    assert discovered.status_code == 200, discovered.text
    assert discovered.json() == {
        "default_namespace": "cafe_sales",
        "namespaces": ["admin", "cafe_sales", "reporting"],
        "complete": True,
        "warning": None,
    }
    assert captured == {
        "connection_url": "mongodb://127.0.0.1:27017",
        "host": None,
        "port": 27017,
        "user": "",
        "password": "",
    }


def test_namespace_discovery_keeps_manual_fallback_when_enumeration_is_denied(
    db_session, monkeypatch
):
    from datasources.mongodb import MongoLoadError

    def denied(**_kwargs):
        raise MongoLoadError("not authorized to execute listDatabases")

    monkeypatch.setattr("api.connection_dispatch.list_mongodb_databases", denied)
    client = _client(db_session, monkeypatch)
    cid = client.post(
        "/api/connections",
        json={
            "connector_id": "mongodb",
            "connection": {
                "connection_url": "mongodb://127.0.0.1:27017",
                "database": "cafe_sales",
            },
        },
    ).json()["id"]

    discovered = client.get(f"/api/connections/{cid}/namespaces").json()

    assert discovered["default_namespace"] == "cafe_sales"
    assert discovered["namespaces"] == ["cafe_sales"]
    assert discovered["complete"] is False
    assert "manually" in discovered["warning"]


async def test_namespace_dispatch_covers_every_connector(monkeypatch):
    import api.connection_dispatch as dispatch

    cases = [
        ("postgres", "list_postgres_schemas", {"host": "pg"}, "public", "analytics"),
        ("sqlserver", "list_sqlserver_schemas", {"host": "sql"}, "dbo", "sales"),
        (
            "mongodb",
            "list_mongodb_databases",
            {"host": "mongo", "database": "app"},
            "app",
            "archive",
        ),
        (
            "snowflake",
            "list_snowflake_schemas",
            {"database": "WAREHOUSE"},
            "PUBLIC",
            "FINANCE",
        ),
        ("redshift", "list_redshift_schemas", {"database": "dev"}, "public", "mart"),
        (
            "bigquery",
            "list_bigquery_datasets",
            {"project_id": "project"},
            "",
            "events",
        ),
        (
            "databricks",
            "list_databricks_schemas",
            {"catalog": "main"},
            "default",
            "gold",
        ),
    ]

    for connector, function_name, credentials, default, discovered in cases:
        monkeypatch.setattr(dispatch, function_name, lambda *_args, **_kwargs: [discovered])
        preferred, names, complete, warning = await dispatch.list_namespaces(
            connector,
            credentials,
            config={"datasources": {connector: {"enabled": True}}},
            user="user-1",
            db=None,
        )
        assert preferred == default
        assert discovered in names
        assert complete is True
        assert warning is None


def test_pin_table_helper_requires_relation(db_session):
    row = create_connection(
        db_session,
        app_user_id="user-1",
        connector_id="postgres",
        name="Local",
        credentials={"use_default": True},
        hint={"use_default": True},
    )
    with pytest.raises(ValueError):
        pin_table(
            db_session,
            row,
            schema_name="public",
            table_name=None,
            custom_query=None,
            display_name="x",
        )


def _snowflake_body():
    return {
        "connector_id": "snowflake",
        "name": "Analytics",
        "connection": {
            "account": "org-account",
            "user": "analyst",
            "private_key": _PEM,
            "warehouse": "COMPUTE_WH",
            "database": "DB",
        },
    }


class _CascadeManager:
    def __init__(self):
        self.states = {}

    def list_sessions(self, app_user_id=None):
        return [
            sid
            for sid, state in self.states.items()
            if app_user_id is None or state.metadata.get("user_id") == app_user_id
        ]

    def get_session(self, session_id, app_user_id=None):
        return self.states.get(session_id)

    def mark_dirty(self, session_id):
        return None

    def mark_snapshot_clean(self, session_id):
        return None

    async def snapshot_data_async(self, session, raise_on_error=False):
        return None


def test_connection_usage_and_delete_requires_confirm(db_session, monkeypatch):
    from datetime import datetime

    from agents.state import AgentState, DataContext

    manager = _CascadeManager()
    monkeypatch.setattr("api.connection_routes.state_manager", manager)
    monkeypatch.setattr("api.source_lifecycle.state_manager", manager)

    client = _client(db_session, monkeypatch)
    cid = client.post("/api/connections", json=_snowflake_body()).json()["id"]
    df = pd.DataFrame({"id": [1]})
    ctx = DataContext(
        file_id="live-file",
        filename="DB.PUBLIC.T",
        raw_dataframe=df.copy(),
        working_dataframe=df.copy(),
        uploaded_at=datetime.utcnow(),
        metadata={
            "source": "snowflake",
            "connection_id": cid,
            "connection_config": {"private_key": _PEM},
        },
    )
    session = AgentState(session_id="live-chat", metadata={"user_id": "user-1"})
    session.add_data_context(ctx)
    manager.states["live-chat"] = session

    usage = client.get(f"/api/connections/{cid}/usage")
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["table_count"] == 1
    assert body["sessions"][0]["tables"] == ["DB.PUBLIC.T"]
    etag = body["etag"]

    blocked = client.delete(f"/api/connections/{cid}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "confirm_required"
    assert session.data_context is not None

    stale = client.delete(f"/api/connections/{cid}", params={"confirm": True, "etag": "nope"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_usage"

    deleted = client.delete(f"/api/connections/{cid}", params={"confirm": True, "etag": etag})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["detached"][0]["file_id"] == "live-file"
    assert session.data_context is None
    assert get_connection(db_session, "user-1", cid) is None


def test_connection_delete_retires_snapshot_only_session(tmp_path, db_session, monkeypatch):
    from datetime import datetime

    from agents.state import state_manager as real_state
    from database.session_store import ensure_session_row
    from helpers.storage import LocalStorageBackend, StorageManager

    storage = StorageManager(
        LocalStorageBackend(
            upload_dir=str(tmp_path / "uploads"),
            snapshot_dir=str(tmp_path / "snapshots"),
        )
    )
    monkeypatch.setattr("api.source_lifecycle.storage_manager", storage)
    monkeypatch.setattr("api.connection_routes.storage_manager", storage)
    monkeypatch.setattr("helpers.storage.storage_manager", storage)
    monkeypatch.setattr("api.source_lifecycle.state_manager", real_state)
    monkeypatch.setattr("api.connection_routes.state_manager", real_state)

    client = _client(db_session, monkeypatch)
    cid = client.post("/api/connections", json=_snowflake_body()).json()["id"]
    session_id = "snap-chat"
    file_id = "snap-file"
    ensure_session_row(
        db_session, session_id=session_id, app_user_id="user-1", title="Warehouse chat"
    )
    db_session.commit()

    df = pd.DataFrame({"id": [1, 2]})
    buf = __import__("io").BytesIO()
    df.to_parquet(buf, index=False)
    storage.save_snapshot_object(session_id, f"{file_id}.parquet", buf.getvalue())
    storage.save_snapshot_object(
        session_id,
        f"{file_id}.meta.json",
        json.dumps(
            {
                "file_id": file_id,
                "filename": "DB.PUBLIC.ORDERS",
                "uploaded_at": datetime.utcnow().isoformat(),
                "metadata": {
                    "source": "snowflake",
                    "connection_id": cid,
                    "connection_config": {"private_key": _PEM, "account": "org-account"},
                },
            }
        ).encode(),
    )
    storage.save_snapshot_object(
        session_id,
        "__manifest__.json",
        json.dumps(
            {
                "session_id": session_id,
                "user_id": "user-1",
                "primary_file_id": file_id,
                "additional_file_ids": [],
            }
        ).encode(),
    )

    usage = client.get(f"/api/connections/{cid}/usage")
    assert usage.status_code == 200, usage.text
    assert usage.json()["table_count"] == 1
    etag = usage.json()["etag"]

    deleted = client.delete(f"/api/connections/{cid}", params={"confirm": True, "etag": etag})
    assert deleted.status_code == 200, deleted.text
    assert storage.load_snapshot_object(session_id, f"{file_id}.meta.json") is None
    assert get_connection(db_session, "user-1", cid) is None
    hydrated = real_state.states.get(session_id)
    if hydrated is not None:
        assert hydrated.data_context is None
    real_state.states.pop(session_id, None)


# ---------------------------------------------------------------------------
# Tags, health status, and the connector catalog
# ---------------------------------------------------------------------------


def test_normalize_tags_dedupes_trims_and_caps():
    from database.connection_store import normalize_tags

    assert normalize_tags(["  Prod ", "prod", "FINANCE", ""]) == ["prod", "finance"]
    assert normalize_tags(["x" * 50])[0] == "x" * 32
    assert len(normalize_tags([f"tag{i}" for i in range(40)])) == 20
    assert normalize_tags([None, 3, "ok"]) == ["ok"]  # type: ignore[list-item]


def test_create_and_patch_round_trip_tags_and_description(db_session, monkeypatch):
    client = _client(db_session, monkeypatch)
    created = client.post(
        "/api/connections",
        json={
            "connector_id": "snowflake",
            "name": "Warehouse",
            "description": "Finance mart",
            "tags": ["Prod", "prod", " Finance "],
            "connection": {"account": "org", "user": "svc"},
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["tags"] == ["prod", "finance"]
    assert body["description"] == "Finance mart"
    # A connection is only persisted after a successful test, so it starts healthy.
    assert body["last_test_status"] == "ok"
    assert body["last_used_at"] is None

    patched = client.patch(
        f"/api/connections/{body['id']}",
        json={"tags": ["staging"], "description": ""},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["tags"] == ["staging"]
    assert patched.json()["description"] is None


def test_failed_test_records_unhealthy_status(db_session, monkeypatch):
    client = _client(db_session, monkeypatch)
    cid = client.post(
        "/api/connections",
        json={"connector_id": "snowflake", "connection": {"account": "org"}},
    ).json()["id"]

    async def failing_test(connector_id, params, **_kwargs):
        raise HTTPException(status_code=400, detail="could not connect to host")

    monkeypatch.setattr("api.connection_routes.test_and_normalize", failing_test)
    resp = client.post(f"/api/connections/{cid}/test", json={})
    assert resp.status_code == 400

    row = get_connection(db_session, "user-1", cid)
    assert row.last_test_status == "error"
    assert "could not connect" in row.last_test_error

    # Recovering flips it back to healthy and clears the recorded error.
    async def ok_test(connector_id, params, **_kwargs):
        return dict(params)

    monkeypatch.setattr("api.connection_routes.test_and_normalize", ok_test)
    assert client.post(f"/api/connections/{cid}/test", json={}).status_code == 200
    db_session.refresh(row)
    assert row.last_test_status == "ok"
    assert row.last_test_error is None


def test_catalog_lists_connector_types_with_configured_counts(db_session, monkeypatch):
    client = _client(db_session, monkeypatch)
    client.post(
        "/api/connections",
        json={"connector_id": "snowflake", "connection": {"account": "org"}},
    )

    resp = client.get("/api/connections/catalog")
    assert resp.status_code == 200, resp.text
    entries = {c["id"]: c for c in resp.json()["connectors"]}

    assert entries["snowflake"]["configured_count"] == 1
    assert entries["postgres"]["configured_count"] == 0
    assert entries["bigquery"]["vendor"] == "gcp"
    assert entries["redshift"]["vendor"] == "aws"
    assert entries["mongodb"]["relation_noun"] == "collection"
    assert entries["bigquery"]["schema_label"] == "Dataset"
    assert entries["postgres"]["label"] == "PostgreSQL"
    joins = entries["mongodb"]["same_source_join"]
    assert {item["strategy"] for item in joins["strategies"]} >= {
        "exact",
        "levenshtein",
        "metaphone",
    }
    assert joins["join_types"] == ["INNER", "LEFT", "RIGHT", "FULL OUTER"]
    lexical = next(item for item in joins["strategies"] if item["strategy"] == "levenshtein")
    assert lexical["scope"] == "full"
    assert lexical["egress"] == "full"
    # Connectors absent from the config default to enabled rather than vanishing.
    assert entries["mongodb"]["enabled"] is True


def test_catalog_route_is_not_shadowed_by_connection_id(db_session, monkeypatch):
    """`/catalog` must resolve before the `/{connection_id}` wildcard."""
    client = _client(db_session, monkeypatch)
    assert client.get("/api/connections/catalog").status_code == 200
    assert client.get("/api/connections/not-a-real-id").status_code == 404

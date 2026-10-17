"""Saved-filter persistence: store CRUD, version history, and the routes."""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.dependencies import get_current_user
from database import filter_store
from database.connection import Base, get_db
from filters.routes import router as filter_router

SPEC = {"name": "negatives", "select": {"op": "lt", "column": "balance", "value": 0}}


@pytest.fixture()
def db_session():
    # StaticPool shares ONE in-memory connection across threads — required
    # because TestClient serves sync endpoints from a threadpool, and the
    # default per-thread pool would hand each thread a fresh empty database.
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


# ------------------------------------------------------------------ store


def test_create_and_get(db_session):
    row = filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="data.csv"
    )
    assert row.version == 1 and row.name == "negatives"
    got = filter_store.get_filter(db_session, row.id, "u1")
    assert got.id == row.id
    d = filter_store.filter_to_dict(got)
    assert d["spec"]["select"]["op"] == "lt" and d["dataset_ref"] == "data.csv"


def test_ownership_enforced(db_session):
    row = filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="d"
    )
    with pytest.raises(PermissionError):
        filter_store.get_filter(db_session, row.id, "intruder")


def test_update_bumps_version_and_records_history(db_session):
    row = filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="d"
    )
    new_spec = {**SPEC, "name": "renamed"}
    updated = filter_store.update_filter(db_session, row.id, "u1", spec=new_spec)
    assert updated.version == 2 and updated.name == "renamed"
    versions = filter_store.list_versions(db_session, row.id, "u1")
    assert [v.version_n for v in versions] == [1, 2]


def test_restore_creates_new_head(db_session):
    row = filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="d"
    )
    filter_store.update_filter(db_session, row.id, "u1", spec={**SPEC, "name": "v2"})
    restored = filter_store.restore_version(db_session, row.id, "u1", 1)
    assert restored.version == 3 and restored.name == "negatives"


def test_list_scopes_by_dataset_ref_and_soft_delete(db_session):
    a = filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="a.csv"
    )
    filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="b.csv"
    )
    assert len(filter_store.list_filters(db_session, "s1", "u1")) == 2
    only_a = filter_store.list_filters(db_session, "s1", "u1", dataset_ref="a.csv")
    assert [r.id for r in only_a] == [a.id]
    filter_store.soft_delete_filter(db_session, a.id, "u1")
    assert filter_store.list_filters(db_session, "s1", "u1", dataset_ref="a.csv") == []
    assert filter_store.get_filter(db_session, a.id, "u1") is None


def test_soft_delete_for_dataset_matches_prefix(db_session):
    keep = filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="other"
    )
    filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="file-1"
    )
    filter_store.create_filter(
        db_session, session_id="s1", app_user_id="u1", spec=SPEC, dataset_ref="file-1::base"
    )
    assert filter_store.soft_delete_for_dataset(db_session, "s1", "u1", "file-1") == 2
    remaining = filter_store.list_filters(db_session, "s1", "u1")
    assert [row.id for row in remaining] == [keep.id]


# ------------------------------------------------------------------ routes


class _FakeContext:
    def __init__(self, df, file_id="f1", filename="data.csv"):
        self.dataframe = df
        self.file_id = file_id
        self.filename = filename
        self.metadata = {}


class _FakeSession:
    def __init__(self, ctxs):
        self._ctxs = ctxs
        self.data_context = ctxs[0] if ctxs else None
        self.metadata = {}

    def get_all_data_contexts(self):
        return list(self._ctxs)


class _FakeStateManager:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, session_id, app_user_id=None):
        return self.sessions.get((session_id, app_user_id))


@pytest.fixture()
def client(db_session, tmp_path, monkeypatch):
    import filters.routes as fr

    df = pd.DataFrame({"id": range(4), "balance": [-5.0, 10.0, -1.0, 3.0]})
    df.to_csv(tmp_path / "data.csv", index=False)

    class _FakeWorkspaceManager:
        def get_or_create(self, session_id, user_id=None):
            return tmp_path

    import api.preview_service as ps

    fake_state = _FakeStateManager({("s1", "user-1"): _FakeSession([_FakeContext(df)])})
    monkeypatch.setattr(fr, "state_manager", fake_state)
    monkeypatch.setattr(ps, "WorkspaceManager", _FakeWorkspaceManager)

    app = FastAPI()
    app.include_router(filter_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_crud_roundtrip(client):
    created = client.post(
        "/api/filters/sessions/s1",
        json={"spec": SPEC, "dataset_ref": "data.csv"},
    )
    assert created.status_code == 201
    fid = created.json()["id"]

    listed = client.get("/api/filters/sessions/s1", params={"dataset_ref": "data.csv"})
    assert [f["id"] for f in listed.json()["filters"]] == [fid]

    updated = client.put(f"/api/filters/{fid}", json={"spec": {**SPEC, "name": "v2"}})
    assert updated.status_code == 200 and updated.json()["version"] == 2

    versions = client.get(f"/api/filters/{fid}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]

    restored = client.post(f"/api/filters/{fid}/restore/1")
    assert restored.status_code == 200 and restored.json()["version"] == 3

    assert client.delete(f"/api/filters/{fid}").status_code == 204
    assert client.get(f"/api/filters/{fid}").status_code == 404


def test_list_named_copy_includes_legacy_temp_filter(client):
    import filters.routes as fr

    ctx = fr.state_manager.sessions[("s1", "user-1")].data_context
    ctx.metadata = {
        "working_copies": [
            {
                "name": "databricks_space_columns",
                "table": "working_1",
                "parent": "source",
                "role": "working",
            }
        ],
        "active_copy": "databricks_space_columns",
        "temp_schema": "stych_temp",
        "temp_table": "working_1",
    }
    fid = client.post(
        "/api/filters/sessions/s1",
        json={"spec": SPEC, "dataset_ref": "f1::temp"},
    ).json()["id"]

    listed = client.get(
        "/api/filters/sessions/s1",
        params={"dataset_ref": "f1::copy:databricks_space_columns"},
    )

    assert [row["id"] for row in listed.json()["filters"]] == [fid]
    other = client.get(
        "/api/filters/sessions/s1",
        params={"dataset_ref": "f1::copy:other"},
    )
    assert other.json()["filters"] == []


def test_create_invalid_spec_422(client):
    r = client.post(
        "/api/filters/sessions/s1",
        json={"spec": {"name": ""}, "dataset_ref": "d"},
    )
    assert r.status_code == 422
    assert r.json()["errors"]


def test_apply_saved_filter(client):
    fid = client.post(
        "/api/filters/sessions/s1",
        json={"spec": SPEC, "dataset_ref": "data.csv"},
    ).json()["id"]
    r = client.get(
        "/api/filters/sessions/s1/apply",
        params={
            "filter_id": fid,
            "source": "file",
            "path": "data.csv",
            "matches_only": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filtered_rows"] == 2
    assert body["summary"]["matched_rows"] == 2


def test_apply_missing_filter_404(client):
    r = client.get(
        "/api/filters/sessions/s1/apply",
        params={"filter_id": "nope", "source": "file", "path": "data.csv"},
    )
    assert r.status_code == 404


def test_foreign_users_filter_403(client, db_session):
    row = filter_store.create_filter(
        db_session, session_id="s2", app_user_id="someone-else", spec=SPEC, dataset_ref="d"
    )
    assert client.get(f"/api/filters/{row.id}").status_code == 403

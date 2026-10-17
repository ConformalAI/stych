"""Filter preview endpoint: stubbed auth/state like test_preview_diff."""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.dependencies import get_current_user
from filters.routes import _active_sql_variant
from filters.routes import router as filter_router


class _FakeContext:
    def __init__(self, df, file_id="f1", filename="cafe.csv", metadata=None):
        self.dataframe = df
        self.working_dataframe = df
        self.file_id = file_id
        self.filename = filename
        self.metadata = metadata or {}


class _FakeSession:
    def __init__(self, ctxs):
        self.session_id = "s1"
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


def test_dataset_filter_targets_the_active_sql_working_copy():
    ctx = _FakeContext(
        pd.DataFrame({"id": [1]}),
        metadata={"temp_schema": "STYCH_TEMP", "temp_table": "working_1"},
    )
    assert _active_sql_variant(ctx) == "temp"
    ctx.metadata.pop("temp_table")
    assert _active_sql_variant(ctx) == "base"


class _FakeWorkspaceManager:
    root = None

    def get_or_create(self, session_id, user_id=None):
        return type(self).root


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import api.preview_service as ps
    import execution.session_plane as sp
    import filters.routes as fr

    df = pd.DataFrame(
        {"id": range(4), "balance": [-5.0, 10.0, -1.0, 3.0], "name": ["a", "b", "a", "c"]}
    )
    df.to_csv(tmp_path / "data.csv", index=False)
    df.to_parquet(tmp_path / "data.parquet", index=False)

    _FakeWorkspaceManager.root = tmp_path
    remote = _FakeContext(
        df,
        file_id="remote",
        filename="remote.csv",
        metadata={
            "source": "sqlserver",
            "snapshot_row_limit": 10_000,
            "snapshot_truncated": False,
        },
    )
    fake_state = _FakeStateManager({("s1", "user-1"): _FakeSession([_FakeContext(df), remote])})
    monkeypatch.setattr(fr, "state_manager", fake_state)
    monkeypatch.setattr(ps, "WorkspaceManager", _FakeWorkspaceManager)
    monkeypatch.setattr(sp, "WorkspaceManager", _FakeWorkspaceManager)

    app = FastAPI()
    app.include_router(filter_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    return TestClient(app)


def _preview(client, spec, **kwargs):
    return client.post(
        "/api/filters/sessions/s1/preview",
        json={"spec": spec, "source": "file", "path": "data.csv", **kwargs},
    )


def test_preview_inline_spec(client):
    r = _preview(
        client,
        {
            "name": "negatives",
            "select": {"op": "lt", "column": "balance", "value": 0},
            "present": {"rules": [{"id": "neg", "severity": "danger"}]},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "filter"
    assert body["summary"]["matched_rows"] == 2
    assert body["summary"]["rule_counts"] == {"neg": 2}
    assert body["summary"]["identity_resolved"] == "key:id"


def test_preview_dataset_source(client):
    r = client.post(
        "/api/filters/sessions/s1/preview",
        json={
            "spec": {"name": "t", "select": {"op": "eq", "column": "name", "value": "a"}},
            "source": "dataset",
            "file_id": "f1",
            "matches_only": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filtered_rows"] == 2
    assert body["engine"] == "duckdb"
    assert body["scope"] == "full"


def test_preview_dataset_executes_duckdb_regex_predicate(client):
    r = client.post(
        "/api/filters/sessions/s1/preview",
        json={
            "spec": {
                "name": "case-insensitive regex",
                "select": {"op": "regex", "column": "name", "pattern": "^A$"},
            },
            "source": "dataset",
            "file_id": "f1",
            "matches_only": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["filtered_rows"] == 2


def test_preview_dataset_annotation_falls_back_with_identical_semantics(client):
    r = client.post(
        "/api/filters/sessions/s1/preview",
        json={
            "spec": {
                "name": "duplicates",
                "annotate": [{"kind": "duplicate_values", "id": "dupes", "columns": ["name"]}],
                "select": {"op": "gt", "column": "dupes_count", "value": 1},
            },
            "source": "dataset",
            "file_id": "f1",
            "matches_only": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filtered_rows"] == 2
    name_index = [column["name"] for column in body["columns"]].index("name")
    assert [row[name_index] for row in body["rows"]] == ["a", "a"]


def test_preview_invalid_spec_422(client):
    r = _preview(client, {"name": "", "select": {"op": "bogus"}})
    assert r.status_code == 422
    errs = r.json()["errors"]
    assert errs and {"path", "code", "message"} <= set(errs[0])


def test_preview_unknown_select_column_400(client):
    r = _preview(client, {"name": "t", "select": {"op": "eq", "column": "nope", "value": 1}})
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


def test_preview_duckdb_unknown_select_column_400(client):
    r = client.post(
        "/api/filters/sessions/s1/preview",
        json={
            "spec": {"name": "t", "select": {"op": "eq", "column": "nope", "value": 1}},
            "source": "dataset",
            "file_id": "f1",
        },
    )
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


def test_preview_remote_filter_without_live_credentials_requires_reconnect(client):
    r = client.post(
        "/api/filters/sessions/s1/preview",
        json={"spec": {"name": "t"}, "source": "dataset", "file_id": "remote"},
    )
    assert r.status_code == 409
    assert "unavailable" in str(r.json()["detail"]).lower()


def test_preview_sql_source_uses_duckdb_for_non_postgres_base(client):
    r = client.post(
        "/api/filters/sessions/s1/preview",
        json={"spec": {"name": "t"}, "source": "sql", "source_id": "f1::base"},
    )
    assert r.status_code == 200
    assert r.json()["total_rows"] == 4


def test_preview_wrong_session_404(client):
    r = client.post(
        "/api/filters/sessions/nope/preview",
        json={"spec": {"name": "t"}, "source": "file", "path": "data.csv"},
    )
    assert r.status_code == 404

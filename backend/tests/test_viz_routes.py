"""Endpoint tests for the viz REST API: query loop, CRUD, versions, restore."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.dependencies import get_config, get_current_user, get_current_user_record, get_orchestrator
from database.connection import get_db

_DF = pd.DataFrame(
    {
        "order_date": pd.to_datetime(["2024-01-05", "2024-01-20", "2024-02-10", "2024-02-15"]),
        "region": ["EU", "NA", "EU", "APAC"],
        "revenue": [100.0, 250.5, 80.0, 20.0],
    }
)

_BAR_SPEC = {
    "mark": {"type": "bar"},
    "encoding": {
        "x": {"field": "region", "type": "nominal"},
        "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
    },
}


class _FakeDataContext:
    def __init__(self, df, file_id="f1", filename="sales.csv"):
        self.file_id = file_id
        self.filename = filename
        self.raw_dataframe = df
        self.working_dataframe = df.copy()
        self.uploaded_at = datetime.utcnow()
        self.metadata: dict = {}

    def is_sql_datasource(self) -> bool:
        return False

    def get_sql_target(self):
        return (None, None)


class _FakeSession:
    def __init__(self, df):
        self.session_id = "s1"
        self.data_context = _FakeDataContext(df)
        self.additional_data_contexts = []
        self.metadata: dict = {}

    def get_all_data_contexts(self):
        return ([self.data_context] if self.data_context is not None else []) + list(
            self.additional_data_contexts
        )


class _FakeStateManager:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, session_id, app_user_id=None):
        return self.sessions.get((session_id, app_user_id))


class _FakeOrchestrator:
    sql_agent = None


@pytest.fixture()
def db_session(tmp_path):
    """File-backed sqlite session usable across TestClient's worker threads
    (in-memory sqlite is one-DB-per-thread, so routes would see empty tables)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.connection import Base

    engine = create_engine(
        f"sqlite:///{tmp_path}/viz_test.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_client(monkeypatch, db_session, *, user: str, sessions: dict) -> TestClient:
    import viz.routes as vr

    monkeypatch.setattr(vr, "state_manager", _FakeStateManager(sessions))
    app = FastAPI()
    app.include_router(vr.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_record] = lambda: type(
        "User", (), {"email": f"{user}@test.local"}
    )()
    app.dependency_overrides[get_config] = lambda: {"usage_limit": {"enabled": "false"}}
    app.dependency_overrides[get_orchestrator] = lambda: _FakeOrchestrator()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


@pytest.fixture()
def client(monkeypatch, db_session):
    with _make_client(
        monkeypatch, db_session, user="user-1", sessions={("s1", "user-1"): _FakeSession(_DF)}
    ) as c:
        yield c


@pytest.fixture()
def other_user_client(monkeypatch, db_session):
    # Same session map as `client` (the module attribute is patched once per
    # test); lookups are keyed by user, so user-2 still can't see s1.
    with _make_client(
        monkeypatch, db_session, user="user-2", sessions={("s1", "user-1"): _FakeSession(_DF)}
    ) as c:
        yield c


def _ensure_user(db_session, user_id="user-1"):
    from database.models import AppUser

    if db_session.query(AppUser).filter(AppUser.id == user_id).first() is None:
        db_session.add(
            AppUser(
                id=user_id,
                supertokens_user_id=f"st-{user_id}",
                email=f"{user_id}@test.local",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        db_session.flush()


# ---------------------------------------------------------------------------


def test_catalog(client):
    r = client.get("/api/viz/sessions/s1/catalog")
    assert r.status_code == 200
    fields = {f["name"]: f for f in r.json()["fields"]}
    assert fields["region"]["type"] == "nominal"
    assert fields["revenue"]["role"] == "measure"
    assert r.json()["source"] == "f1"


def test_catalog_unknown_session_404(client):
    assert client.get("/api/viz/sessions/nope/catalog").status_code == 404


def test_catalog_empty_session_is_source_removed(monkeypatch, db_session):
    state = _FakeSession(_DF)
    state.data_context = None
    with _make_client(
        monkeypatch, db_session, user="user-1", sessions={("s1", "user-1"): state}
    ) as c:
        r = c.get("/api/viz/sessions/s1/catalog")
        assert r.status_code == 410
        assert r.json()["detail"] == {
            "code": "source_removed",
            "message": "This dataset was removed from the chat.",
        }


def test_query_returns_normalized_spec_and_data(client):
    r = client.post("/api/viz/sessions/s1/query", json={"spec": _BAR_SPEC})
    assert r.status_code == 200
    body = r.json()
    assert body["spec"]["schema_version"] == 1
    assert [c["name"] for c in body["data"]["columns"]] == ["x", "y"]
    assert body["data"]["rows"][0] == ["NA", 250.5]
    assert body["data"]["meta"]["engine"] == "duckdb"


def test_query_round_trips_explicit_color_and_label_config(client):
    spec = {
        **_BAR_SPEC,
        "config": {
            "color": {
                "mode": "categorical",
                "binding": {"channel": "x", "field": "region"},
                "palette": ["#2563EB", "#0FB981"],
                "assignments": [{"value": "EU", "color": "#DC2626"}],
            },
            "labels": True,
            "labelFontSize": 14,
            "labelColor": "#F9FAFB",
        },
    }

    response = client.post("/api/viz/sessions/s1/query", json={"spec": spec})

    assert response.status_code == 200
    config = response.json()["spec"]["config"]
    assert config["color"]["assignments"] == [{"value": "EU", "color": "#DC2626"}]
    assert config["labelFontSize"] == 14
    assert config["labelColor"] == "#F9FAFB"


def test_query_bad_spec_422_with_errors(client):
    bad = {
        "mark": {"type": "bar"},
        "encoding": {
            "x": {"field": "regian", "type": "nominal"},
            "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
        },
    }
    r = client.post("/api/viz/sessions/s1/query", json={"spec": bad})
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert errors[0]["code"] == "unknown_field"
    assert "region" in errors[0]["message"]


def test_query_malformed_spec_422(client):
    r = client.post("/api/viz/sessions/s1/query", json={"spec": {"mark": {"type": "sunburst"}}})
    assert r.status_code == 422


def test_crud_and_versions_flow(client, db_session):
    _ensure_user(db_session)

    # Create
    r = client.post(
        "/api/viz/sessions/s1/visualizations",
        json={"spec": _BAR_SPEC, "title": "Revenue by region"},
    )
    assert r.status_code == 201, r.text
    viz = r.json()["visualization"]
    viz_id = viz["id"]
    assert viz["version"] == 1
    assert viz["title"] == "Revenue by region"
    assert viz["dataset_ref"] == "f1"
    assert viz["spec"]["data"]["source"] == "f1"
    assert r.json()["data"]["rows"][0] == ["NA", 250.5]

    # List
    r = client.get("/api/viz/sessions/s1/visualizations")
    assert [v["id"] for v in r.json()["visualizations"]] == [viz_id]

    # Update spec → version 2
    spec2 = {**_BAR_SPEC, "mark": {"type": "bar", "stacked": True}}
    r = client.put(
        f"/api/viz/visualizations/{viz_id}",
        json={"spec": spec2, "expected_version": 1},
    )
    assert r.status_code == 200
    assert r.json()["visualization"]["version"] == 2
    assert r.json()["visualization"]["spec"]["mark"]["stacked"] is True

    # Versions
    r = client.get(f"/api/viz/visualizations/{viz_id}/versions")
    versions = r.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["spec"]["mark"]["stacked"] is False

    # Materialize saved spec
    r = client.get(f"/api/viz/visualizations/{viz_id}/data")
    assert r.status_code == 200
    assert r.json()["data"]["rows"][0] == ["NA", 250.5]

    # Restore v1 → creates v3 with v1's spec
    r = client.post(f"/api/viz/visualizations/{viz_id}/restore/1?expected_version=2")
    assert r.status_code == 200
    restored = r.json()["visualization"]
    assert restored["version"] == 3
    assert restored["spec"]["mark"]["stacked"] is False

    # Delete → gone from list and 404 on fetch
    r = client.delete(f"/api/viz/visualizations/{viz_id}")
    assert r.json()["deleted"] is True
    assert client.get(f"/api/viz/visualizations/{viz_id}").status_code == 404
    assert client.get("/api/viz/sessions/s1/visualizations").json()["visualizations"] == []


def test_crud_preserves_complete_chart_configuration(client, db_session):
    _ensure_user(db_session)
    rich_config = {
        "color": {
            "mode": "categorical",
            "value": "#111827",
            "binding": {"channel": "x", "field": "region"},
            "palette": ["#2563EB", "#0FB981"],
            "assignments": [{"value": "EU", "color": "#DC2626"}],
        },
        "texture": {"enabled": True, "pattern": "crosshatch"},
        "legend": False,
        "grid": False,
        "labels": True,
        "labelFontSize": 16,
        "labelColor": "#F9FAFB",
        "numberFormat": ",.1f",
    }
    created = client.post(
        "/api/viz/sessions/s1/visualizations",
        json={"spec": {**_BAR_SPEC, "config": rich_config}, "title": "Styled chart"},
    )
    assert created.status_code == 201, created.text
    viz_id = created.json()["visualization"]["id"]
    stored = created.json()["visualization"]["spec"]["config"]
    for key, value in rich_config.items():
        assert stored[key] == value

    updated_config = {**rich_config, "grid": True, "labelColor": "#111827"}
    updated = client.put(
        f"/api/viz/visualizations/{viz_id}",
        json={
            "spec": {**_BAR_SPEC, "config": updated_config},
            "title": "Styled chart",
            "expected_version": 1,
        },
    )
    assert updated.status_code == 200, updated.text
    fetched = client.get(f"/api/viz/visualizations/{viz_id}").json()["visualization"]
    for key, value in updated_config.items():
        assert fetched["spec"]["config"][key] == value

    versions = client.get(f"/api/viz/visualizations/{viz_id}/versions").json()["versions"]
    assert versions[0]["spec"]["config"]["grid"] is False
    assert versions[0]["spec"]["config"]["labelColor"] == "#F9FAFB"
    assert versions[1]["spec"]["config"]["grid"] is True
    assert versions[1]["spec"]["config"]["labelColor"] == "#111827"

    restored = client.post(f"/api/viz/visualizations/{viz_id}/restore/1?expected_version=2").json()[
        "visualization"
    ]
    for key, value in rich_config.items():
        assert restored["spec"]["config"][key] == value


def test_saved_viz_does_not_follow_primary_dataset(monkeypatch, db_session):
    _ensure_user(db_session)
    state = _FakeSession(_DF)
    other = _FakeDataContext(
        _DF.assign(revenue=[1000.0, 2000.0, 3000.0, 4000.0]),
        file_id="f2",
        filename="other.csv",
    )
    state.additional_data_contexts.append(other)
    with _make_client(
        monkeypatch,
        db_session,
        user="user-1",
        sessions={("s1", "user-1"): state},
    ) as c:
        created = c.post(
            "/api/viz/sessions/s1/visualizations",
            json={"spec": {**_BAR_SPEC, "data": {"source": "f2"}}},
        )
        assert created.status_code == 201, created.text
        viz_id = created.json()["visualization"]["id"]
        assert created.json()["visualization"]["dataset_ref"] == "f2"

        # Changing session focus cannot redirect the persisted spec.
        state.data_context, state.additional_data_contexts = other, [state.data_context]
        refreshed = c.get(f"/api/viz/visualizations/{viz_id}/data")
        assert refreshed.status_code == 200
        assert sum(row[1] for row in refreshed.json()["data"]["rows"]) == 10000.0

        # Removing its exact source keeps the chart but reports it as unavailable.
        state.data_context = state.additional_data_contexts[0]
        state.additional_data_contexts = []
        unavailable = c.get(f"/api/viz/visualizations/{viz_id}/data")
        assert unavailable.status_code == 410
        assert unavailable.json()["detail"]["code"] == "source_removed"
        assert unavailable.json()["detail"]["message"] == "This dataset was removed from the chat."
        listed = c.get("/api/viz/sessions/s1/visualizations").json()["visualizations"]
        assert listed[0]["source_available"] is False


def test_update_rejects_stale_version(client, db_session):
    _ensure_user(db_session)
    created = client.post(
        "/api/viz/sessions/s1/visualizations",
        json={"spec": _BAR_SPEC},
    ).json()["visualization"]
    viz_id = created["id"]
    first = client.put(
        f"/api/viz/visualizations/{viz_id}",
        json={"spec": _BAR_SPEC, "expected_version": 1},
    )
    assert first.status_code == 200

    stale = client.put(
        f"/api/viz/visualizations/{viz_id}",
        json={"spec": _BAR_SPEC, "expected_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "message": "Visualization changed from version 1 to 2. Reload it before saving again.",
        "expected_version": 1,
        "current_version": 2,
    }

    stale_restore = client.post(f"/api/viz/visualizations/{viz_id}/restore/1?expected_version=1")
    assert stale_restore.status_code == 409
    assert stale_restore.json()["detail"]["code"] == "version_conflict"


def test_create_rejects_invalid_spec(client, db_session):
    _ensure_user(db_session)
    bad = {**_BAR_SPEC, "encoding": {"x": {"field": "nope", "type": "nominal"}}}
    r = client.post("/api/viz/sessions/s1/visualizations", json={"spec": bad})
    assert r.status_code == 422
    assert client.get("/api/viz/sessions/s1/visualizations").json()["visualizations"] == []


def test_ownership_enforced(client, other_user_client, db_session):
    _ensure_user(db_session, "user-1")
    r = client.post("/api/viz/sessions/s1/visualizations", json={"spec": _BAR_SPEC})
    viz_id = r.json()["visualization"]["id"]

    assert other_user_client.get(f"/api/viz/visualizations/{viz_id}").status_code == 403
    assert other_user_client.delete(f"/api/viz/visualizations/{viz_id}").status_code == 403


# ---------------------------------------------------------------------------
# Calculated fields + ask-this-chart endpoints
# ---------------------------------------------------------------------------


def test_validate_calculations_endpoint(client):
    r = client.post(
        "/api/viz/sessions/s1/calculations/validate",
        json={
            "transform": [
                {"op": "filter", "field": "region", "predicate": "notnull"},
                {"op": "calculate", "as": "rev2", "expr": "revenue * 2"},
                {"op": "calculate", "as": "bad", "expr": "revenue +"},
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert [f["name"] for f in body["fields"]] == ["rev2"]
    assert body["fields"][0]["is_calculated"] is True
    assert body["fields"][0]["role"] == "measure"
    assert len(body["errors"]) == 1 and body["errors"][0]["code"] == "expr_invalid"
    assert body["errors"][0]["path"] == "transform[2].expr"


def test_validate_calculations_uses_selected_dataset(monkeypatch, db_session):
    state = _FakeSession(_DF)
    state.additional_data_contexts.append(
        _FakeDataContext(
            pd.DataFrame({"units": [1, 2], "price": [3.0, 4.0]}),
            file_id="f2",
            filename="inventory.csv",
        )
    )
    with _make_client(
        monkeypatch,
        db_session,
        user="user-1",
        sessions={("s1", "user-1"): state},
    ) as c:
        r = c.post(
            "/api/viz/sessions/s1/calculations/validate",
            json={
                "source": "f2",
                "transform": [{"op": "calculate", "as": "value", "expr": "units * price"}],
            },
        )

    assert r.status_code == 200
    assert [field["name"] for field in r.json()["fields"]] == ["value"]
    assert r.json()["errors"] == []


def test_query_with_calculated_field(client):
    r = client.post(
        "/api/viz/sessions/s1/query",
        json={
            "spec": {
                "mark": {"type": "bar"},
                "transform": [{"op": "calculate", "as": "rev2", "expr": "revenue * 2"}],
                "encoding": {
                    "x": {"field": "region", "type": "nominal"},
                    "y": {"field": "rev2", "type": "quantitative", "aggregate": "sum"},
                },
            }
        },
    )
    assert r.status_code == 200
    rows = r.json()["data"]["rows"]
    assert rows[0] == ["NA", 501.0]  # 250.5 * 2
    # normalized spec keeps the calculate entry (wire key "as")
    assert r.json()["spec"]["transform"][0]["as"] == "rev2"


class _StubGraphAgent:
    async def generate_response(self, messages, temperature=0.2):
        return '{"headline": "EU leads", "insight": "EU holds most revenue.", "drill_spec": null}'


@pytest.fixture()
def ask_client(monkeypatch, db_session):
    import viz.routes as vr

    class _Orch:
        sql_agent = None
        graph_agent = _StubGraphAgent()

    monkeypatch.setattr(
        vr, "state_manager", _FakeStateManager({("s1", "user-1"): _FakeSession(_DF)})
    )
    app = FastAPI()
    app.include_router(vr.router)
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    app.dependency_overrides[get_current_user_record] = lambda: type(
        "User", (), {"email": "user-1@test.local"}
    )()
    app.dependency_overrides[get_config] = lambda: {"usage_limit": {"enabled": "false"}}
    app.dependency_overrides[get_orchestrator] = lambda: _Orch()
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setattr(
        vr,
        "get_usage_tracker",
        lambda: type("Tracker", (), {"is_within_quota": lambda self, **_: (True, None)})(),
    )
    with TestClient(app) as c:
        yield c


def test_ask_selection_endpoint(ask_client):
    r = ask_client.post(
        "/api/viz/sessions/s1/ask",
        json={
            "spec": _BAR_SPEC,
            "selection": {"items": [{"channel": "x", "values": ["EU"]}]},
            "mode": "explain",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["headline"] == "EU leads"
    assert body["stats"]["selected_points"] == 1
    assert body["drill"] is None


def test_ask_selection_rejects_measure_channel(ask_client):
    r = ask_client.post(
        "/api/viz/sessions/s1/ask",
        json={
            "spec": _BAR_SPEC,
            "selection": {"items": [{"channel": "y", "values": [100]}]},
            "mode": "explain",
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["code"] == "selection_on_measure"


def test_ask_selection_rejects_user_over_quota(ask_client, monkeypatch):
    import viz.routes as vr

    monkeypatch.setattr(
        vr,
        "get_usage_tracker",
        lambda: type(
            "Tracker",
            (),
            {"is_within_quota": lambda self, **_: (False, "Usage limit reached")},
        )(),
    )

    r = ask_client.post(
        "/api/viz/sessions/s1/ask",
        json={
            "spec": _BAR_SPEC,
            "selection": {"items": [{"channel": "x", "values": ["EU"]}]},
        },
    )

    assert r.status_code == 402
    assert r.json()["detail"] == "Usage limit reached"


def test_ask_selection_sets_usage_session_context(ask_client, monkeypatch):
    import viz.routes as vr

    class _Tracker:
        session_id = None
        user_id = None

        def set_session_id(self, session_id):
            self.session_id = session_id

        def set_user_id(self, user_id):
            self.user_id = user_id

    tracker = _Tracker()
    monkeypatch.setattr(vr, "get_langfuse_tracker", lambda: tracker)

    r = ask_client.post(
        "/api/viz/sessions/s1/ask",
        json={
            "spec": _BAR_SPEC,
            "selection": {"items": [{"channel": "x", "values": ["EU"]}]},
        },
    )

    assert r.status_code == 200
    assert tracker.session_id == "s1"
    assert tracker.user_id == "user-1"


async def test_viz_request_limit_rejects_another_active_request(monkeypatch):
    import viz.routes as vr

    blocker = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(vr, "_MAX_CONCURRENT_VIZ_REQUESTS_PER_USER", 1)
    monkeypatch.setattr(vr, "_viz_request_tasks", {"user-1": {blocker}})
    try:
        with pytest.raises(HTTPException) as exc_info:
            vr._acquire_viz_request_slot("user-1")
        assert exc_info.value.status_code == 429
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)

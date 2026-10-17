"""Ontology REST API: the editor's contract with the backend."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents.state import DataContext
from api.dependencies import get_current_user, get_orchestrator
from database.connection import Base, get_db
from ontology.routes import router as ontology_router

USER = "user-1"
SESSION = "s1"
ORDERS = "file-orders"
CUSTOMERS = "file-customers"


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


class FakeState:
    def __init__(self):
        orders = pd.DataFrame(
            {
                "order_id": [1, 2, 3],
                "customer_id": [10, 11, 10],
                "order_total": [100.0, 250.0, 80.0],
                "status": ["shipped", "cancelled", "shipped"],
            }
        )
        customers = pd.DataFrame({"id": [10, 11], "region": ["EU", "NA"]})
        self.data_context = _ctx(ORDERS, "orders.csv", orders)
        self.additional_data_contexts = [_ctx(CUSTOMERS, "customers.csv", customers)]
        self.session_id = SESSION
        self.metadata = {"user_id": USER}

    def get_all_data_contexts(self):
        return [self.data_context, *self.additional_data_contexts]


def _ctx(file_id, filename, df):
    return DataContext(
        file_id=file_id,
        filename=filename,
        raw_dataframe=df,
        working_dataframe=df,
        uploaded_at=datetime.now(timezone.utc),
        metadata={},
    )


@pytest.fixture()
def client(db_session, monkeypatch):
    import ontology.routes as routes

    state = FakeState()

    class _FakeStateManager:
        def get_session(self, session_id, app_user_id=None):
            return state if session_id == SESSION else None

    monkeypatch.setattr(routes, "state_manager", _FakeStateManager())

    app = FastAPI()
    app.include_router(ontology_router)
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_orchestrator] = lambda: None
    return TestClient(app)


SPEC = {
    "schemaVersion": 1,
    "entities": [
        {"name": "orders", "dataset": ORDERS, "primaryKey": "order_id"},
        {"name": "customers", "dataset": CUSTOMERS, "primaryKey": "id"},
    ],
    "relationships": [
        {
            "name": "order_customer",
            "fromEntity": "orders",
            "fromField": "customer_id",
            "toEntity": "customers",
            "toField": "id",
        }
    ],
    "dimensions": [{"name": "region", "entity": "customers", "field": "region"}],
    "metrics": [
        {
            "name": "revenue",
            "entity": "orders",
            "expr": "order_total",
            "aggregate": "sum",
            "filterExpr": "status != 'cancelled'",
        }
    ],
}


# ---------------------------------------------------------------------------
# GET / PUT
# ---------------------------------------------------------------------------


def test_get_seeds_an_empty_ontology(client):
    r = client.get(f"/api/ontology?session_id={SESSION}")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["spec"]["entities"] == []
    assert body["errors"] == []


def test_get_lists_session_datasets_with_columns(client):
    body = client.get(f"/api/ontology?session_id={SESSION}").json()
    datasets = {d["id"]: d for d in body["datasets"]}
    assert set(datasets) == {ORDERS, CUSTOMERS}
    assert datasets[ORDERS]["filename"] == "orders.csv"
    names = [f["name"] for f in datasets[ORDERS]["fields"]]
    assert "order_total" in names


def test_reading_without_session_context_still_works(client):
    """The ontology is per user, so it opens with no session context at all.
    Reading must not 404 — that made the Ontology tab unopenable."""
    r = client.get("/api/ontology")
    assert r.status_code == 200
    body = r.json()
    assert body["spec"]["entities"] == []
    assert body["datasets"] == []
    assert body["errors"] == []


def test_a_stored_ontology_is_not_reported_broken_when_state_is_missing(client, db_session):
    """Without datasets to check against, silence beats inventing errors."""
    from database import ontology_store

    row = ontology_store.get_or_create(db_session, USER)
    ontology_store.save_spec(db_session, row.id, USER, SPEC)
    body = client.get("/api/ontology").json()
    assert [e["name"] for e in body["spec"]["entities"]] == ["orders", "customers"]
    assert body["errors"] == []


def test_writing_without_session_context_is_allowed(client):
    """An ontology is editable without data loaded; only running a metric needs
    a session. Requiring one to save would make it un-editable between
    sessions, which is the whole point of user scope."""
    r = client.put("/api/ontology", json={"spec": SPEC})
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["spec"]["entities"]] == ["orders", "customers"]


def test_the_ontology_is_shared_across_sessions(client):
    """Same user, different session — same ontology. This is the behaviour the
    per-user scope exists for."""
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    body = client.get("/api/ontology?session_id=some-other-session").json()
    assert [e["name"] for e in body["spec"]["entities"]] == ["orders", "customers"]


def test_save_roundtrip_and_version_bump(client):
    saved = client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    assert saved.status_code == 200
    assert saved.json()["version"] == 2
    assert saved.json()["errors"] == []

    reloaded = client.get(f"/api/ontology?session_id={SESSION}").json()
    assert [m["name"] for m in reloaded["spec"]["metrics"]] == ["revenue"]
    assert reloaded["version"] == 2


def test_save_fills_dataset_labels(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    spec = client.get(f"/api/ontology?session_id={SESSION}").json()["spec"]
    labels = {e["name"]: e["datasetLabel"] for e in spec["entities"]}
    assert labels == {"orders": "orders.csv", "customers": "customers.csv"}


def test_a_broken_spec_still_saves_but_reports_errors(client):
    """The editor must be able to hold work in progress."""
    broken = {
        **SPEC,
        "metrics": [{"name": "bad", "entity": "orders", "expr": "nope", "aggregate": "sum"}],
    }
    r = client.put(f"/api/ontology?session_id={SESSION}", json={"spec": broken})
    assert r.status_code == 200
    assert any(e["code"] == "unknown_field" for e in r.json()["errors"])
    # …and it really was persisted.
    assert (
        client.get(f"/api/ontology?session_id={SESSION}").json()["spec"]["metrics"][0]["name"]
        == "bad"
    )


def test_schema_violations_are_422(client):
    r = client.put(
        f"/api/ontology?session_id={SESSION}",
        json={"spec": {"entities": [{"name": "Not Snake Case", "dataset": ORDERS}]}},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]


def test_stale_expected_version_is_409(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    r = client.put(
        f"/api/ontology?session_id={SESSION}", json={"spec": SPEC, "expected_version": 1}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "version_conflict"
    assert r.json()["detail"]["current_version"] == 2


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def test_query_returns_the_metric_value(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    r = client.post(f"/api/ontology/sessions/{SESSION}/query", json={"metrics": ["revenue"]})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["rows"] == [[180.0]]  # the cancelled 250 is excluded
    assert body["uncertified"] == ["revenue"]
    assert body["spans_entities"] is False


def test_query_joins_across_entities(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    r = client.post(
        f"/api/ontology/sessions/{SESSION}/query",
        json={"metrics": ["revenue"], "dimensions": [{"name": "region"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["spans_entities"] is True
    # NA's only order is cancelled, so every row's folded aggregate is null and
    # the group sums to NULL — not 0. That mirrors SQL SUM, and it is the honest
    # answer: "no qualifying rows" is not the same claim as "revenue was zero".
    assert dict(body["data"]["rows"]) == {"EU": 180.0, "NA": None}


def test_query_with_an_unknown_metric_is_422(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    r = client.post(f"/api/ontology/sessions/{SESSION}/query", json={"metrics": ["profit"]})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["code"] == "unknown_metric"


def test_query_caps_the_number_of_dimensions(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    r = client.post(
        f"/api/ontology/sessions/{SESSION}/query",
        json={"metrics": ["revenue"], "dimensions": [{"name": "region"}] * 5},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Certify / versions / proposals
# ---------------------------------------------------------------------------


def test_certify_flips_named_definitions(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    r = client.post("/api/ontology/certify", json={"names": ["revenue", "orders"]})
    assert r.status_code == 200
    assert r.json()["changed"] == 2
    spec = r.json()["spec"]
    assert spec["metrics"][0]["certified"] is True
    assert spec["entities"][0]["certified"] is True
    assert spec["entities"][1]["certified"] is False


def test_certify_is_idempotent(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    client.post("/api/ontology/certify", json={"names": ["revenue"]})
    again = client.post("/api/ontology/certify", json={"names": ["revenue"]})
    assert again.json()["changed"] == 0


def test_certified_metric_is_no_longer_flagged_in_a_query(client):
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    client.post("/api/ontology/certify", json={"names": ["revenue"]})
    r = client.post(f"/api/ontology/sessions/{SESSION}/query", json={"metrics": ["revenue"]})
    assert r.json()["uncertified"] == []


def test_versions_and_restore(client):
    ontology_id = client.get(f"/api/ontology?session_id={SESSION}").json()["id"]
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": SPEC})
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": {**SPEC, "metrics": []}})

    versions = client.get(f"/api/ontology/{ontology_id}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]

    restored = client.post(f"/api/ontology/{ontology_id}/restore/2")
    assert restored.status_code == 200
    assert restored.json()["version"] == 4
    assert [m["name"] for m in restored.json()["spec"]["metrics"]] == ["revenue"]


def test_restoring_a_missing_version_is_404(client):
    ontology_id = client.get(f"/api/ontology?session_id={SESSION}").json()["id"]
    assert client.post(f"/api/ontology/{ontology_id}/restore/99").status_code == 404


def test_accept_proposal_certifies_only_the_proposed_names(client, db_session):
    from database import ontology_store

    ontology_id = client.get(f"/api/ontology?session_id={SESSION}").json()["id"]
    ontology_store.save_spec(
        db_session,
        ontology_id,
        USER,
        SPEC,
        author="agent",
        proposed=["revenue"],
    )
    version = ontology_store.get_ontology(db_session, ontology_id, USER).version

    r = client.post(f"/api/ontology/{ontology_id}/proposals/{version}/accept")
    assert r.status_code == 200
    assert r.json()["accepted"] == ["revenue"]
    spec = r.json()["spec"]
    assert spec["metrics"][0]["certified"] is True
    # The entities were not part of the proposal, so they stay drafts.
    assert all(e["certified"] is False for e in spec["entities"])


def test_reject_proposal_rolls_back_to_the_parent_version(client, db_session):
    from database import ontology_store

    ontology_id = client.get(f"/api/ontology?session_id={SESSION}").json()["id"]
    # v2: a human-authored baseline with no metrics.
    client.put(f"/api/ontology?session_id={SESSION}", json={"spec": {**SPEC, "metrics": []}})
    # v3: the agent proposes revenue.
    ontology_store.save_spec(
        db_session, ontology_id, USER, SPEC, author="agent", proposed=["revenue"]
    )

    r = client.post(f"/api/ontology/{ontology_id}/proposals/3/reject")
    assert r.status_code == 200
    assert r.json()["spec"]["metrics"] == []


def test_rejecting_the_first_version_is_refused(client):
    ontology_id = client.get(f"/api/ontology?session_id={SESSION}").json()["id"]
    assert client.post(f"/api/ontology/{ontology_id}/proposals/1/reject").status_code == 422


def test_another_users_ontology_is_403(client, db_session, monkeypatch):
    ontology_id = client.get(f"/api/ontology?session_id={SESSION}").json()["id"]
    client.app.dependency_overrides[get_current_user] = lambda: "someone-else"
    assert client.get(f"/api/ontology/{ontology_id}/versions").status_code == 403

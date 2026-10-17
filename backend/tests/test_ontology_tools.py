"""Agent-facing ontology tools: listing, querying, and human-gated proposals."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents.state import DataContext
from database import ontology_store
from database.connection import Base
from ontology.spec import OntologySpec
from tools import ontology_tools
from tools.ontology_tools import OntologyToolRegistry, render_ontology_text

USER = "user-1"
SESSION = "session-1"
ORDERS = "file-orders"
CUSTOMERS = "file-customers"


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _session_local(engine, monkeypatch):
    """Point the tools' SessionLocal at the in-memory test database."""
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(ontology_tools, "SessionLocal", factory)
    return factory


class FakeEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, agent, message, **kwargs):
        self.events.append({"agent": agent, "message": message, **kwargs})


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
        self.data_context = self._ctx(ORDERS, "orders.csv", orders)
        self.additional_data_contexts = [self._ctx(CUSTOMERS, "customers.csv", customers)]
        self.session_id = SESSION
        self.metadata = {"user_id": USER}

    @staticmethod
    def _ctx(file_id, filename, df):
        return DataContext(
            file_id=file_id,
            filename=filename,
            raw_dataframe=df,
            working_dataframe=df,
            uploaded_at=datetime.now(timezone.utc),
            metadata={},
        )

    def get_all_data_contexts(self):
        return [self.data_context, *self.additional_data_contexts]


@pytest.fixture()
def state():
    return FakeState()


@pytest.fixture()
def registry():
    return OntologyToolRegistry(progress_emitter=FakeEmitter())


def _seed(engine, spec: dict) -> str:
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_or_create(db, USER)
        ontology_store.save_spec(db, row.id, USER, spec, expected_version=1)
        return row.id
    finally:
        db.close()


REVENUE_SPEC = {
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
            "certified": True,
        }
    ],
}


# ---------------------------------------------------------------------------
# list_ontology
# ---------------------------------------------------------------------------


async def test_list_ontology_on_empty_model_points_at_proposing(state, registry):
    out = await registry.call("list_ontology", {}, state)
    assert "No semantic model defined yet" in out
    # The datasets are still listed so the agent can propose against real ids.
    assert ORDERS in out and "order_total" in out


async def test_list_ontology_marks_drafts_and_certified(engine, state, registry):
    spec = json.loads(json.dumps(REVENUE_SPEC))
    spec["metrics"].append(
        {"name": "order_count", "entity": "orders", "aggregate": "count", "certified": False}
    )
    _seed(engine, spec)
    out = await registry.call("list_ontology", {}, state)
    assert "revenue [certified]" in out
    assert "order_count [DRAFT]" in out
    assert "orders.customer_id → customers.id" in out


def test_render_includes_value_synonyms():
    spec = OntologySpec.model_validate(
        {
            "entities": [{"name": "customers", "dataset": CUSTOMERS}],
            "dimensions": [
                {
                    "name": "region",
                    "entity": "customers",
                    "field": "region",
                    "valueSynonyms": {"CA": ["California"]},
                }
            ],
        }
    )
    assert "California" in render_ontology_text(spec, [])


# ---------------------------------------------------------------------------
# query_metric
# ---------------------------------------------------------------------------


async def test_query_metric_applies_the_stored_filter(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call("query_metric", {"metrics": ["revenue"]}, state)
    assert '"revenue": 180.0' in out  # 100 + 80; the cancelled 250 is excluded


async def test_query_metric_joins_via_a_relationship(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call(
        "query_metric", {"metrics": ["revenue"], "dimensions": [{"name": "region"}]}, state
    )
    assert "joined via the ontology's relationships to: customers" in out
    assert "EU" in out


async def test_query_metric_accepts_bare_string_dimensions(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call(
        "query_metric", {"metrics": ["revenue"], "dimensions": ["region"]}, state
    )
    assert "EU" in out


async def test_query_metric_warns_loudly_about_drafts(engine, state, registry):
    spec = json.loads(json.dumps(REVENUE_SPEC))
    spec["metrics"][0]["certified"] = False
    _seed(engine, spec)
    out = await registry.call("query_metric", {"metrics": ["revenue"]}, state)
    assert "UNCERTIFIED" in out and "revenue" in out


async def test_query_metric_certified_answer_has_no_warning(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call("query_metric", {"metrics": ["revenue"]}, state)
    assert "UNCERTIFIED" not in out


async def test_query_metric_unknown_name_is_a_fixable_observation(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call("query_metric", {"metrics": ["profit"]}, state)
    assert out.startswith("INVALID") and "profit" in out


async def test_query_metric_without_metrics_explains_itself(state, registry):
    out = await registry.call("query_metric", {"metrics": []}, state)
    assert "list_ontology" in out


# ---------------------------------------------------------------------------
# propose_ontology
# ---------------------------------------------------------------------------


async def test_proposals_are_always_uncertified(engine, state, registry):
    out = await registry.call(
        "propose_ontology",
        {
            "entities": [{"name": "orders", "dataset": ORDERS, "primaryKey": "order_id"}],
            "metrics": [
                {
                    "name": "revenue",
                    "entity": "orders",
                    "expr": "order_total",
                    "aggregate": "sum",
                    # The agent asking to certify must not be honoured.
                    "certified": True,
                }
            ],
            "intent": "user asked about revenue",
        },
        state,
    )
    assert "DRAFTS" in out
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        spec = OntologySpec.model_validate(json.loads(row.spec_json))
    finally:
        db.close()
    assert spec.metric("revenue").certified is False
    assert spec.metric("revenue").provenance.author == "agent"
    assert spec.metric("revenue").provenance.intent == "user asked about revenue"


async def test_proposal_emits_an_ontology_event(engine, state, registry):
    await registry.call(
        "propose_ontology",
        {
            "entities": [{"name": "orders", "dataset": ORDERS}],
            "metrics": [{"name": "n", "entity": "orders", "aggregate": "count"}],
        },
        state,
    )
    event = registry._progress_emitter.events[-1]
    assert event["event_type"] == "ontology"
    assert event["action"] == "proposed"
    assert set(event["proposed"]) == {"orders", "n"}
    assert event["session_id"] == SESSION


async def test_proposal_records_names_for_the_review_card(engine, state, registry):
    await registry.call(
        "propose_ontology",
        {
            "entities": [{"name": "orders", "dataset": ORDERS}],
            "metrics": [{"name": "n", "entity": "orders", "aggregate": "count"}],
        },
        state,
    )
    assert registry.proposals[0]["proposed"] == ["orders", "n"]
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        head = ontology_store.list_versions(db, row.id, USER)[0]
    finally:
        db.close()
    assert json.loads(head.proposed_json) == ["orders", "n"]


async def test_invalid_proposal_is_rejected_before_it_persists(engine, state, registry):
    out = await registry.call(
        "propose_ontology",
        {
            "entities": [{"name": "orders", "dataset": ORDERS}],
            "metrics": [
                {"name": "bad", "entity": "orders", "expr": "no_such_column", "aggregate": "sum"}
            ],
        },
        state,
    )
    assert out.startswith("INVALID")
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        spec = OntologySpec.model_validate(json.loads(row.spec_json))
    finally:
        db.close()
    assert spec.metrics == []


async def test_revising_a_draft_updates_it_in_place(engine, state, registry):
    """The follow-up loop: "actually, exclude cancelled orders too"."""
    spec = json.loads(json.dumps(REVENUE_SPEC))
    spec["metrics"][0]["certified"] = False
    spec["metrics"][0].pop("filterExpr")
    _seed(engine, spec)

    out = await registry.call(
        "propose_ontology",
        {
            "metrics": [
                {
                    "name": "revenue",
                    "entity": "orders",
                    "expr": "order_total",
                    "aggregate": "sum",
                    "filterExpr": "status != 'cancelled'",
                }
            ]
        },
        state,
    )
    assert "revised revenue" in out
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        revised = OntologySpec.model_validate(json.loads(row.spec_json))
    finally:
        db.close()
    assert revised.metric("revenue").filter_expr == "status != 'cancelled'"
    assert revised.metric("revenue").certified is False


async def test_revising_a_certified_definition_is_refused(engine, state, registry):
    """Certification is the human's sign-off; the agent may not overwrite it."""
    _seed(engine, REVENUE_SPEC)  # revenue is certified
    out = await registry.call(
        "propose_ontology",
        {"metrics": [{"name": "revenue", "entity": "orders", "expr": "1", "aggregate": "sum"}]},
        state,
    )
    assert "Refused" in out and "certified" in out
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        unchanged = OntologySpec.model_validate(json.loads(row.spec_json))
    finally:
        db.close()
    assert unchanged.metric("revenue").expr == "order_total"


async def test_removing_a_draft_drops_it(engine, state, registry):
    spec = json.loads(json.dumps(REVENUE_SPEC))
    spec["metrics"][0]["certified"] = False
    _seed(engine, spec)

    out = await registry.call("propose_ontology", {"remove": ["revenue"]}, state)
    assert "removed revenue" in out
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        after = OntologySpec.model_validate(json.loads(row.spec_json))
    finally:
        db.close()
    assert after.metric("revenue") is None


async def test_removing_a_certified_definition_is_refused(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call("propose_ontology", {"remove": ["revenue"]}, state)
    assert "Refused" in out
    db = sessionmaker(bind=engine)()
    try:
        row = ontology_store.get_for_user(db, USER)
        after = OntologySpec.model_validate(json.loads(row.spec_json))
    finally:
        db.close()
    assert after.metric("revenue") is not None


async def test_an_empty_proposal_says_nothing_happened(engine, state, registry):
    _seed(engine, REVENUE_SPEC)
    out = await registry.call("propose_ontology", {}, state)
    assert "Nothing to propose" in out


async def test_unknown_tool_name_is_reported(state, registry):
    assert "Unknown tool" in await registry.call("nope", {}, state)

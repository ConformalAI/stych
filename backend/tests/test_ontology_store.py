"""Ontology persistence: CAS versioning, ownership, history, restore."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import ontology_store
from database.connection import Base
from database.ontology_store import VersionConflictError

USER = "user-1"
OTHER = "user-2"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _spec(metric_names=("revenue",)) -> dict:
    return {
        "schemaVersion": 1,
        "entities": [{"name": "orders", "dataset": "file-1"}],
        "relationships": [],
        "dimensions": [],
        "metrics": [
            {"name": n, "entity": "orders", "expr": "total", "aggregate": "sum"}
            for n in metric_names
        ],
    }


def test_get_or_create_seeds_an_empty_spec(db):
    row = ontology_store.get_or_create(db, USER)
    assert row.version == 1
    assert json.loads(row.spec_json)["metrics"] == []


def test_get_or_create_is_idempotent(db):
    first = ontology_store.get_or_create(db, USER)
    second = ontology_store.get_or_create(db, USER)
    assert first.id == second.id


def test_save_bumps_version_and_records_history(db):
    row = ontology_store.get_or_create(db, USER)
    saved = ontology_store.save_spec(
        db, row.id, USER, _spec(), expected_version=1, summary="added revenue"
    )
    assert saved.version == 2
    versions = ontology_store.list_versions(db, row.id, USER)
    assert [v.version_n for v in versions] == [2, 1]
    assert versions[0].summary == "added revenue"


def test_stale_expected_version_conflicts(db):
    row = ontology_store.get_or_create(db, USER)
    ontology_store.save_spec(db, row.id, USER, _spec(), expected_version=1)
    with pytest.raises(VersionConflictError) as exc:
        ontology_store.save_spec(db, row.id, USER, _spec(("churn",)), expected_version=1)
    assert exc.value.expected == 1 and exc.value.current == 2


def test_save_without_expected_version_skips_the_check(db):
    row = ontology_store.get_or_create(db, USER)
    ontology_store.save_spec(db, row.id, USER, _spec())
    assert ontology_store.get_ontology(db, row.id, USER).version == 2


def test_reads_enforce_ownership(db):
    row = ontology_store.get_or_create(db, USER)
    with pytest.raises(PermissionError):
        ontology_store.get_ontology(db, row.id, OTHER)


def test_other_users_session_lookup_returns_nothing(db):
    ontology_store.get_or_create(db, USER)
    assert ontology_store.get_for_user(db, OTHER) is None


def test_agent_proposals_are_recorded_on_the_version(db):
    row = ontology_store.get_or_create(db, USER)
    ontology_store.save_spec(
        db,
        row.id,
        USER,
        _spec(("revenue", "churn")),
        expected_version=1,
        author="agent",
        summary="proposed 2 metrics",
        proposed=["revenue", "churn"],
    )
    head = ontology_store.list_versions(db, row.id, USER)[0]
    assert head.author == "agent"
    assert json.loads(head.proposed_json) == ["revenue", "churn"]


def test_restore_creates_a_new_head_rather_than_rewinding(db):
    row = ontology_store.get_or_create(db, USER)
    ontology_store.save_spec(db, row.id, USER, _spec(("revenue",)), expected_version=1)
    ontology_store.save_spec(db, row.id, USER, _spec(("churn",)), expected_version=2)

    restored = ontology_store.restore_version(db, row.id, USER, 2)
    assert restored.version == 4  # history stays linear
    assert [m["name"] for m in json.loads(restored.spec_json)["metrics"]] == ["revenue"]
    assert [v.version_n for v in ontology_store.list_versions(db, row.id, USER)] == [4, 3, 2, 1]


def test_restoring_a_missing_version_raises(db):
    row = ontology_store.get_or_create(db, USER)
    with pytest.raises(ValueError, match="not found"):
        ontology_store.restore_version(db, row.id, USER, 99)


def test_rejecting_a_proposal_is_restoring_its_parent(db):
    """The flow the chat card uses: propose → reject → back to the prior spec."""
    row = ontology_store.get_or_create(db, USER)
    ontology_store.save_spec(db, row.id, USER, _spec(("revenue",)), expected_version=1)
    ontology_store.save_spec(
        db,
        row.id,
        USER,
        _spec(("revenue", "churn")),
        expected_version=2,
        author="agent",
        proposed=["churn"],
    )
    rejected = ontology_store.restore_version(db, row.id, USER, 2)
    assert [m["name"] for m in json.loads(rejected.spec_json)["metrics"]] == ["revenue"]

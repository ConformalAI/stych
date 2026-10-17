from __future__ import annotations

from types import SimpleNamespace

from filters import execution


def test_mongo_filter_named_copy_resolves_registered_scratch_collection(monkeypatch):
    calls = {}

    class Runner:
        temp_schema = "stych_custom"

        @classmethod
        def from_meta(cls, meta):
            return cls()

    def fake_page(runner, spec, **kwargs):
        calls.update(kwargs)
        return {"summary": {}}

    monkeypatch.setattr("agents.mongo_agent.MongoScratchRunner", Runner)
    monkeypatch.setattr("filters.mongo_service.build_mongo_filter_page", fake_page)

    ctx = SimpleNamespace(
        metadata={
            "source": "mongodb",
            "connection_url": "mongodb://localhost",
            "database": "source_db",
            "collection": "orders",
            "working_copies": [{"name": "review", "table": "stych_review"}],
        }
    )
    result = execution.build_mongo_filter_page(
        ctx,
        object(),
        "copy:review",
        session_id="s",
        page=1,
        page_size=10,
        matches_only=None,
        enabled_rules=None,
        sort_by=None,
        sort_dir="asc",
        search=None,
        filters=None,
    )

    assert result["engine"] == "mongodb"
    assert calls["database"] == "stych_custom"
    assert calls["collection"] == "stych_review"


def test_mongo_filter_named_copy_rejects_non_scratch_collection(monkeypatch):
    class Runner:
        temp_schema = "stych_custom"

        @classmethod
        def from_meta(cls, meta):
            return cls()

    monkeypatch.setattr("agents.mongo_agent.MongoScratchRunner", Runner)
    ctx = SimpleNamespace(
        metadata={
            "source": "mongodb",
            "connection_url": "mongodb://localhost",
            "database": "source_db",
            "collection": "orders",
            "working_copies": [{"name": "bad", "table": "customer_data"}],
        }
    )

    from fastapi import HTTPException

    try:
        execution.build_mongo_filter_page(
            ctx,
            object(),
            "copy:bad",
            session_id="s",
            page=1,
            page_size=10,
            matches_only=None,
            enabled_rules=None,
            sort_by=None,
            sort_dir="asc",
            search=None,
            filters=None,
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "scratch collection" in str(exc.detail)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("unsafe Mongo working-copy collection was accepted")

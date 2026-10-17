from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.auth_routes import session_router
from api.dependencies import get_current_user
from database import viz_store
from database.connection import Base, get_db
from database.models import LLMUsageEvent, SessionHistory


def _make_client(tmp_path, user: str = "user-1"):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sessions.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    db_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    app = FastAPI()
    app.include_router(session_router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db_session
    return app, db_session, engine


def _add_session(db, session_id: str, user_id: str, *, age_days: int = 0, status: str = "active"):
    now = datetime.utcnow() - timedelta(days=age_days)
    row = SessionHistory(
        id=session_id,
        app_user_id=user_id,
        status=status,
        started_at=now,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        total_tokens=0,
        total_credits=0.0,
        is_anonymous=False,
    )
    db.add(row)
    return row


def test_lists_durable_sessions_without_usage_or_messages(tmp_path):
    app, db, engine = _make_client(tmp_path)
    try:
        _add_session(db, "uploaded", "user-1")
        _add_session(db, "visualized", "user-1")
        db.flush()
        viz_store.create_visualization(
            db,
            session_id="visualized",
            app_user_id="user-1",
            spec={"mark": {"type": "bar"}},
            title="Sales",
        )
        db.commit()

        response = TestClient(app).get("/api/sessions", params={"days": 30, "limit": 20})

        assert response.status_code == 200
        sessions = {item["session_id"]: item for item in response.json()["sessions"]}
        assert set(sessions) == {"uploaded", "visualized"}
        assert sessions["uploaded"]["request_count"] == 0
        assert sessions["uploaded"]["total_tokens"] == 0
        assert sessions["visualized"]["estimated_cost_usd"] == 0
        assert response.json()["has_more"] is False
        assert response.json()["window_days"] == 30
    finally:
        db.close()
        engine.dispose()


def test_session_listing_scopes_and_aggregates_usage(tmp_path):
    app, db, engine = _make_client(tmp_path)
    try:
        current = _add_session(db, "current", "user-1")
        current.total_tokens = 42
        _add_session(db, "other-user", "user-2")
        _add_session(db, "old", "user-1", age_days=40)
        _add_session(db, "inactive", "user-1", status="deleted")
        db.add(
            LLMUsageEvent(
                user_id="user-1",
                session_id="current",
                provider="test",
                model="test-model",
                agent_name="test",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                estimated_cost_usd=0.25,
                credits_consumed=25,
            )
        )
        db.commit()

        response = TestClient(app).get("/api/sessions", params={"days": 30, "limit": 20})

        assert response.status_code == 200
        sessions = response.json()["sessions"]
        assert [item["session_id"] for item in sessions] == ["current"]
        assert sessions[0]["request_count"] == 1
        assert sessions[0]["total_tokens"] == 42
        assert sessions[0]["estimated_cost_usd"] == 0.25
        assert sessions[0]["credits_consumed"] == 25
        assert "old" not in [item["session_id"] for item in sessions]
    finally:
        db.close()
        engine.dispose()


def test_lists_all_sessions_when_no_days_window(tmp_path):
    app, db, engine = _make_client(tmp_path)
    try:
        _add_session(db, "current", "user-1")
        _add_session(db, "old", "user-1", age_days=40)
        db.commit()

        response = TestClient(app).get("/api/sessions", params={"limit": 20})

        assert response.status_code == 200
        assert [item["session_id"] for item in response.json()["sessions"]] == [
            "current",
            "old",
        ]
        assert response.json()["window_days"] is None
        assert response.json()["has_more"] is False
    finally:
        db.close()
        engine.dispose()


def test_session_list_keyset_pagination(tmp_path):
    app, db, engine = _make_client(tmp_path)
    try:
        _add_session(db, "newest", "user-1", age_days=0)
        _add_session(db, "middle", "user-1", age_days=1)
        _add_session(db, "oldest", "user-1", age_days=2)
        db.commit()

        first = TestClient(app).get("/api/sessions", params={"limit": 2})
        assert first.status_code == 200
        body = first.json()
        assert [item["session_id"] for item in body["sessions"]] == ["newest", "middle"]
        assert body["has_more"] is True
        assert body["next_before_id"] == "middle"

        second = TestClient(app).get(
            "/api/sessions",
            params={
                "limit": 2,
                "before": body["next_before"],
                "before_id": body["next_before_id"],
            },
        )
        assert second.status_code == 200
        page = second.json()
        assert [item["session_id"] for item in page["sessions"]] == ["oldest"]
        assert page["has_more"] is False
    finally:
        db.close()
        engine.dispose()

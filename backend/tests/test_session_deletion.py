"""Regression tests for durable session deletion."""

from unittest.mock import Mock

import pytest

from agents.state import AgentState, StateManager
from database import filter_store, viz_store
from database.models import (
    AppUser,
    SavedFilter,
    SavedFilterVersion,
    SessionHistory,
    Visualization,
    VisualizationVersion,
)
from database.session_store import append_message, delete_session, ensure_session_row


def test_delete_session_removes_all_owned_filter_and_visualization_records(db_session):
    user_id = "session-delete-user"
    session_id = "session-with-artifacts"
    db_session.add(
        AppUser(
            id=user_id,
            supertokens_user_id="st-session-delete-user",
            email="session-delete@example.com",
        )
    )
    ensure_session_row(db_session, session_id=session_id, app_user_id=user_id)
    append_message(
        db_session,
        session_id=session_id,
        app_user_id=user_id,
        role="user",
        content="Keep this session deletable.",
    )
    saved_filter = filter_store.create_filter(
        db_session,
        session_id=session_id,
        app_user_id=user_id,
        dataset_ref="dataset-1",
        spec={"name": "negative balances"},
    )
    filter_store.update_filter(
        db_session,
        saved_filter.id,
        user_id,
        spec={"name": "negative balances v2"},
    )
    visualization = viz_store.create_visualization(
        db_session,
        session_id=session_id,
        app_user_id=user_id,
        spec={"mark": {"type": "bar"}},
    )
    viz_store.update_visualization(
        db_session,
        visualization.id,
        user_id,
        spec={"mark": {"type": "line"}},
    )
    filter_id = saved_filter.id
    visualization_id = visualization.id
    db_session.commit()

    assert delete_session(db_session, session_id=session_id, app_user_id=user_id) is True
    db_session.commit()

    assert db_session.get(SessionHistory, session_id) is None
    assert db_session.query(SavedFilter).filter_by(session_history_id=session_id).count() == 0
    assert db_session.query(SavedFilterVersion).filter_by(filter_id=filter_id).count() == 0
    assert db_session.query(Visualization).filter_by(session_history_id=session_id).count() == 0
    assert (
        db_session.query(VisualizationVersion).filter_by(visualization_id=visualization_id).count()
        == 0
    )


def test_failed_durable_delete_keeps_cached_session_for_retry(monkeypatch):
    import agents.state as state_module

    manager = StateManager()
    state = AgentState(session_id="session-that-fails", metadata={"user_id": "user-1"})
    manager.states[state.session_id] = state
    manager._persisted_message_counts[state.session_id] = 0

    db = Mock()
    monkeypatch.setattr(state_module, "SessionLocal", lambda: db)

    def fail_delete(*_args, **_kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(state_module, "db_delete_session", fail_delete)

    with pytest.raises(RuntimeError, match="simulated database failure"):
        manager.delete_session(state.session_id, app_user_id="user-1")

    assert manager.states[state.session_id] is state
    assert manager._persisted_message_counts[state.session_id] == 0
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_direct_state_manager_deletion_closes_session_kernel(monkeypatch):
    import execution.duckdb as duckdb_execution
    import helpers.storage as storage_module

    manager = StateManager()
    state = AgentState(session_id="session-with-kernel")
    manager.states[state.session_id] = state
    manager._persisted_message_counts[state.session_id] = 0
    kernel_manager = Mock()
    monkeypatch.setattr(duckdb_execution, "duckdb_kernel_manager", kernel_manager)
    monkeypatch.setattr(
        storage_module.storage_manager, "delete_session_snapshots", Mock(return_value=0)
    )

    assert manager.delete_session(state.session_id) is True

    kernel_manager.close_session.assert_called_once_with(state.session_id, None)


def test_delete_session_wrong_owner_raises_permission_error(monkeypatch):
    import agents.state as state_module

    manager = StateManager()
    db = Mock()
    monkeypatch.setattr(state_module, "SessionLocal", lambda: db)

    def deny(*_args, **_kwargs):
        raise PermissionError("Session belongs to a different user")

    monkeypatch.setattr(state_module, "db_delete_session", deny)

    with pytest.raises(PermissionError, match="different user"):
        manager.delete_session("sid", app_user_id="user-1")

    db.rollback.assert_called_once()
    db.close.assert_called_once()
    assert "sid" not in manager.states


def test_delete_session_in_memory_owner_mismatch_does_not_evict(monkeypatch):
    manager = StateManager()
    state = AgentState(session_id="sid", metadata={"user_id": "user-other"})
    manager.states["sid"] = state

    with pytest.raises(PermissionError, match="different user"):
        manager.delete_session("sid", app_user_id="user-1")

    assert manager.states["sid"] is state


def test_delete_missing_session_returns_false(monkeypatch):
    import agents.state as state_module
    import execution.duckdb as duckdb_execution
    import helpers.storage as storage_module

    manager = StateManager()
    db = Mock()
    monkeypatch.setattr(state_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(state_module, "db_delete_session", lambda *_a, **_k: False)
    monkeypatch.setattr(duckdb_execution, "duckdb_kernel_manager", Mock())
    snap = Mock(return_value=0)
    monkeypatch.setattr(storage_module.storage_manager, "delete_session_snapshots", snap)

    assert manager.delete_session("ghost", app_user_id="user-1") is False
    db.commit.assert_called_once()
    snap.assert_called_once_with("ghost")


def test_delete_session_can_skip_storage_purge(monkeypatch):
    import agents.state as state_module
    import execution.duckdb as duckdb_execution
    import helpers.storage as storage_module

    manager = StateManager()
    db = Mock()
    monkeypatch.setattr(state_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(state_module, "db_delete_session", lambda *_a, **_k: True)
    monkeypatch.setattr(duckdb_execution, "duckdb_kernel_manager", Mock())
    snap = Mock(return_value=0)
    monkeypatch.setattr(storage_module.storage_manager, "delete_session_snapshots", snap)

    assert manager.delete_session("sid", app_user_id="user-1", purge_storage=False) is True
    snap.assert_not_called()


def _delete_client(monkeypatch, *, deleted=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import routes as routes_mod
    from api.dependencies import get_current_user

    monkeypatch.setattr(routes_mod, "peek_in_memory_contexts", lambda *_a, **_k: [])
    monkeypatch.setattr(
        routes_mod.state_manager,
        "delete_session",
        lambda *_a, **_k: deleted,
    )

    scheduled = []

    def _drop_coro(coro):
        scheduled.append(coro)
        coro.close()
        return Mock()

    monkeypatch.setattr(routes_mod.asyncio, "create_task", _drop_coro)

    app = FastAPI()
    app.include_router(routes_mod.router)
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    return TestClient(app), routes_mod, scheduled


def test_delete_route_treats_absent_session_as_success(monkeypatch):
    client, _routes, scheduled = _delete_client(monkeypatch, deleted=False)
    response = client.delete("/api/session/never-persisted")
    assert response.status_code == 200
    assert response.json()["session_id"] == "never-persisted"
    assert scheduled == []


def test_delete_route_purges_only_after_owned_delete(monkeypatch):
    client, _routes, scheduled = _delete_client(monkeypatch, deleted=True)
    response = client.delete("/api/session/real")
    assert response.status_code == 200
    assert len(scheduled) == 1


def test_delete_route_forbidden_for_other_users_session(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import routes as routes_mod
    from api.dependencies import get_current_user

    monkeypatch.setattr(routes_mod, "peek_in_memory_contexts", lambda *_a, **_k: [])

    def deny(*_a, **_k):
        raise PermissionError("Session belongs to a different user")

    monkeypatch.setattr(routes_mod.state_manager, "delete_session", deny)

    app = FastAPI()
    app.include_router(routes_mod.router)
    app.dependency_overrides[get_current_user] = lambda: "user-1"

    response = TestClient(app).delete("/api/session/someone-elses")
    assert response.status_code == 403


def test_delete_route_does_not_hydrate_or_snapshot(monkeypatch):
    client, routes_mod, _scheduled = _delete_client(monkeypatch, deleted=True)

    hydrate = Mock(side_effect=AssertionError("delete must not hydrate"))
    monkeypatch.setattr(routes_mod.state_manager, "get_session", hydrate)

    response = client.delete("/api/session/real")
    assert response.status_code == 200
    hydrate.assert_not_called()


def test_load_snapshot_context_metas_skips_parquet(monkeypatch):
    import json

    from api import session_cleanup as sc

    loaded: list[str] = []

    def load(_sid, name):
        loaded.append(name)
        if name == "__manifest__.json":
            return json.dumps(
                {"user_id": "user-1", "primary_file_id": "f1", "additional_file_ids": []}
            ).encode()
        if name == "f1.meta.json":
            return json.dumps({"file_id": "f1", "metadata": {"source": "snowflake"}}).encode()
        raise AssertionError(f"delete cleanup must not load {name}")

    monkeypatch.setattr(sc.storage_manager, "load_snapshot_object", load)

    ctxs = sc.load_snapshot_context_metas("sid", "user-1")
    assert loaded == ["__manifest__.json", "f1.meta.json"]
    assert ctxs[0]["metadata"]["source"] == "snowflake"


def test_load_snapshot_context_metas_refuses_other_users_manifest(monkeypatch):
    import json

    from api import session_cleanup as sc

    def load(_sid, name):
        if name == "__manifest__.json":
            return json.dumps(
                {"user_id": "owner", "primary_file_id": "f1", "additional_file_ids": []}
            ).encode()
        raise AssertionError("must not read file meta for an unowned snapshot")

    monkeypatch.setattr(sc.storage_manager, "load_snapshot_object", load)
    assert sc.load_snapshot_context_metas("sid", "attacker") == []


def test_load_snapshot_context_metas_refuses_manifest_without_user(monkeypatch):
    import json

    from api import session_cleanup as sc

    def load(_sid, name):
        if name == "__manifest__.json":
            return json.dumps({"primary_file_id": "f1"}).encode()
        raise AssertionError("must not read file meta when snapshot owner is missing")

    monkeypatch.setattr(sc.storage_manager, "load_snapshot_object", load)
    assert sc.load_snapshot_context_metas("sid", "user-1") == []


def test_purge_does_not_delete_unowned_snapshots(monkeypatch):
    from api import session_cleanup as sc

    monkeypatch.setattr(sc, "snapshot_owned_by", lambda *_a, **_k: False)
    monkeypatch.setattr(sc, "load_snapshot_context_metas", lambda *_a, **_k: [])
    monkeypatch.setattr(sc, "cleanup_cleaning_workspace", Mock())
    monkeypatch.setattr(sc, "delete_workspace_archive", Mock())
    monkeypatch.setattr(sc, "_soft_delete_session_uploads", Mock())
    delete_snaps = Mock(return_value=1)
    monkeypatch.setattr(sc.storage_manager, "delete_session_snapshots", delete_snaps)

    import execution.duckdb as duckdb_execution

    monkeypatch.setattr(duckdb_execution, "duckdb_kernel_manager", Mock())

    sc.purge_session_artifacts("sid", "attacker", [])
    delete_snaps.assert_not_called()


def test_purge_does_not_write_or_archive(monkeypatch):
    from api import session_cleanup as sc

    monkeypatch.setattr(sc, "snapshot_owned_by", lambda *_a, **_k: True)
    monkeypatch.setattr(sc, "load_snapshot_context_metas", lambda *_a, **_k: [])
    monkeypatch.setattr(sc, "cleanup_cleaning_workspace", Mock())
    archive = Mock()
    monkeypatch.setattr(sc, "delete_workspace_archive", archive)
    monkeypatch.setattr(sc, "_soft_delete_session_uploads", Mock())
    monkeypatch.setattr(sc.storage_manager, "delete_session_snapshots", Mock(return_value=0))

    import execution.duckdb as duckdb_execution

    monkeypatch.setattr(duckdb_execution, "duckdb_kernel_manager", Mock())

    sc.purge_session_artifacts("sid", "user-1", [])

    archive.assert_called_once_with("sid", "user-1")
    assert not hasattr(sc, "archive_cleaning_workspace")
    assert not hasattr(sc, "snapshot_session")

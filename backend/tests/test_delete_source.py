"""Verify the delete-source service removes a dataset from every location.

Exercises api.routes._delete_dataset against a real local storage backend and a
real per-session workspace: it must drop the in-memory context (promoting the
next dataset), the snapshot objects, the workspace mirror files (local + the
storage archive), the upload object, soft-delete the uploaded_files row, and
repoint active_dataset.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from agents.state import AgentState, DataContext
from api import routes
from helpers.storage import LocalStorageBackend, StorageManager
from workspace.manager import WorkspaceManager, dataset_stem, list_workspace_artifacts


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row

    def all(self):
        return []

    def count(self):
        return 0


class _FakeDb:
    """Minimal stand-in for a SQLAlchemy session returning a single row."""

    def __init__(self, row):
        self._row = row

    def query(self, _model):
        return _FakeQuery(self._row)


def _make_ctx(file_id: str, filename: str) -> DataContext:
    df = pd.DataFrame({"a": [1, 2, 3]})
    return DataContext(
        file_id=file_id,
        filename=filename,
        raw_dataframe=df.copy(deep=True),
        working_dataframe=df.copy(deep=True),
        uploaded_at=datetime.now(),
        metadata={"source": "upload"},
    )


def test_delete_dataset_removes_everywhere(tmp_path, monkeypatch):
    user = "user-1"
    session_id = "sess-del"
    primary_fid = "file-primary"
    extra_fid = "file-extra"

    # Local storage + workspace both anchored in tmp so the test is hermetic.
    storage = StorageManager(
        LocalStorageBackend(
            upload_dir=str(tmp_path / "uploads"),
            temp_dir=str(tmp_path / "temp"),
            snapshot_dir=str(tmp_path / "snapshots"),
            workspace_archive_dir=str(tmp_path / "wsarchive"),
        )
    )
    monkeypatch.setattr(routes, "storage_manager", storage)
    monkeypatch.setenv("WORKSPACE_BASE", str(tmp_path / "workspace"))

    # Session with two datasets; primary is the active one we delete.
    primary = _make_ctx(primary_fid, "sales.csv")
    extra = _make_ctx(extra_fid, "extra.csv")
    session = AgentState(session_id=session_id, metadata={"user_id": user})
    session.add_data_context(primary)
    session.add_data_context(extra)
    stem = dataset_stem(primary.filename)
    session.metadata["active_dataset"] = stem

    # Seed the four persistence locations for the primary dataset.
    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    (workspace / f"{stem}.csv").write_text("a\n1\n")
    (workspace / f"{stem}.parquet").write_bytes(b"parquet-bytes")
    (workspace / "report.md").write_text("# untouched report")  # different stem
    storage.save_workspace_file(user, session_id, f"{stem}.csv", b"a\n1\n")
    storage.save_snapshot_object(session_id, f"{primary_fid}.parquet", b"snap")
    storage.save_snapshot_object(session_id, f"{primary_fid}.meta.json", b"{}")
    storage.save_upload(primary_fid, primary.filename, b"a,b\n1,2\n")

    row = SimpleNamespace(is_deleted=False, deleted_at=None)
    db = _FakeDb(row)

    removed = routes._delete_dataset(session, session_id, user, primary_fid, db)

    assert removed is not None
    assert removed.file_id == primary_fid
    # Context dropped; the extra dataset is promoted to primary.
    assert session.data_context is not None
    assert session.data_context.file_id == extra_fid
    assert primary_fid not in {c.file_id for c in session.get_all_data_contexts()}
    # Snapshot objects gone.
    assert storage.load_snapshot_object(session_id, f"{primary_fid}.parquet") is None
    assert storage.load_snapshot_object(session_id, f"{primary_fid}.meta.json") is None
    # Workspace mirror files gone locally; the unrelated report survives.
    remaining = {a["name"] for a in list_workspace_artifacts(workspace)}
    assert f"{stem}.csv" not in remaining and f"{stem}.parquet" not in remaining
    assert "report.md" in remaining
    # Archived workspace copy gone.
    assert storage.load_workspace_file(user, session_id, f"{stem}.csv") is None
    # Upload object gone.
    assert storage.get_upload_bytes(primary_fid, primary.filename) is None
    # uploaded_files row soft-deleted.
    assert row.is_deleted is True and row.deleted_at is not None
    # active_dataset repointed to the promoted dataset.
    assert session.metadata["active_dataset"] == dataset_stem(extra.filename)


def test_delete_missing_dataset_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(
        routes,
        "storage_manager",
        StorageManager(LocalStorageBackend(snapshot_dir=str(tmp_path / "s"))),
    )
    session = AgentState(session_id="s", metadata={"user_id": "u"})
    session.add_data_context(_make_ctx("only", "only.csv"))
    assert routes._delete_dataset(session, "s", "u", "does-not-exist", _FakeDb(None)) is None


async def test_post_commit_snapshot_retries_without_reversing_deletion(monkeypatch):
    session = SimpleNamespace(session_id="s1", get_all_data_contexts=lambda: [object()])

    class _StateManager:
        def __init__(self):
            self.calls = 0
            self.cleaned = []
            self.dirty = []

        async def snapshot_data_async(self, _session, *, raise_on_error=False):
            self.calls += 1
            if self.calls == 1:
                raise OSError("storage temporarily unavailable")

        def mark_snapshot_clean(self, session_id):
            self.cleaned.append(session_id)

        def mark_dirty(self, session_id):
            self.dirty.append(session_id)

    state_manager = _StateManager()
    monkeypatch.setattr(routes, "state_manager", state_manager)
    monkeypatch.setattr(routes, "_POST_COMMIT_SNAPSHOT_RETRY_DELAYS", (0, 0))

    await routes._repair_source_snapshot(session)

    assert state_manager.calls == 2
    assert state_manager.cleaned == ["s1"]
    assert state_manager.dirty == []


def test_delete_dataset_soft_deletes_viz_and_filters(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import filter_store, viz_store
    from database.connection import Base
    from database.models import AppUser
    from database.session_store import ensure_session_row

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(
        AppUser(
            id="user-1",
            supertokens_user_id="st-user-1",
            email="user-1@test.local",
        )
    )
    db.flush()
    ensure_session_row(db, session_id="sess-del", app_user_id="user-1")

    storage = StorageManager(
        LocalStorageBackend(
            upload_dir=str(tmp_path / "uploads"),
            snapshot_dir=str(tmp_path / "snapshots"),
        )
    )
    monkeypatch.setattr(routes, "storage_manager", storage)

    primary = _make_ctx("file-primary", "sales.csv")
    extra = _make_ctx("file-extra", "extra.csv")
    session = AgentState(session_id="sess-del", metadata={"user_id": "user-1"})
    session.add_data_context(primary)
    session.add_data_context(extra)

    viz_store.create_visualization(
        db,
        session_id="sess-del",
        app_user_id="user-1",
        spec={"mark": {"type": "bar"}, "encoding": {}},
        dataset_ref="file-primary",
    )
    filter_store.create_filter(
        db,
        session_id="sess-del",
        app_user_id="user-1",
        spec={"name": "negatives", "select": {"op": "lt", "column": "a", "value": 0}},
        dataset_ref="file-primary::base",
    )
    filter_store.create_filter(
        db,
        session_id="sess-del",
        app_user_id="user-1",
        spec={"name": "keep", "select": {"op": "gt", "column": "a", "value": 0}},
        dataset_ref="file-extra",
    )

    result = routes._delete_dataset(session, "sess-del", "user-1", "file-primary", db)

    assert result is not None
    assert result.viz_retired == 1
    assert result.filters_retired == 1
    assert viz_store.list_visualizations(db, "sess-del", "user-1") == []
    remaining_filters = filter_store.list_filters(db, "sess-del", "user-1")
    assert [row.dataset_ref for row in remaining_filters] == ["file-extra"]
    assert session.data_context.file_id == "file-extra"
    db.close()
    engine.dispose()

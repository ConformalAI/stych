"""
DB-backed store for visualization entities and their version history.

Every write records a full-spec version row (specs are ~1-2 KB; full copies
beat diff bookkeeping). ``restore_version`` creates a NEW head version with the
old spec so history stays linear. All reads enforce ownership.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.models import Visualization, VisualizationVersion
from database.session_store import ensure_session_row


class VersionConflictError(RuntimeError):
    """A visualization changed after the caller began editing it."""

    def __init__(self, viz_id: str, expected: int, current: int):
        self.viz_id = viz_id
        self.expected = int(expected)
        self.current = int(current)
        super().__init__(
            f"Visualization changed from version {self.expected} to {self.current}. "
            "Reload it before saving again."
        )


def viz_to_dict(row: Visualization) -> dict:
    return {
        "id": row.id,
        "session_id": row.session_history_id,
        "dataset_ref": row.dataset_ref,
        "title": row.title,
        "spec": json.loads(row.spec_json),
        "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def version_to_dict(row: VisualizationVersion) -> dict:
    return {
        "version": row.version_n,
        "spec": json.loads(row.spec_json),
        "author": row.author,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def get_visualization(db: Session, viz_id: str, app_user_id: str) -> Optional[Visualization]:
    row = (
        db.query(Visualization)
        .filter(Visualization.id == viz_id, Visualization.is_deleted.is_(False))
        .first()
    )
    if row is None:
        return None
    if row.app_user_id != app_user_id:
        raise PermissionError("Visualization belongs to a different user")
    return row


def list_visualizations(db: Session, session_id: str, app_user_id: str) -> List[Visualization]:
    return (
        db.query(Visualization)
        .filter(
            Visualization.session_history_id == session_id,
            Visualization.app_user_id == app_user_id,
            Visualization.is_deleted.is_(False),
        )
        .order_by(Visualization.created_at.asc())
        .all()
    )


def create_visualization(
    db: Session,
    *,
    session_id: str,
    app_user_id: str,
    spec: dict,
    title: Optional[str] = None,
    dataset_ref: Optional[str] = None,
    author: str = "user",
) -> Visualization:
    # A viz can be created before any message is persisted — make sure the
    # session row exists so the FK holds.
    ensure_session_row(db, session_id=session_id, app_user_id=app_user_id)
    now = datetime.utcnow()
    spec_json = json.dumps(spec, separators=(",", ":"), default=str)
    row = Visualization(
        app_user_id=app_user_id,
        session_history_id=session_id,
        dataset_ref=dataset_ref,
        title=title or spec.get("title"),
        spec_json=spec_json,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    db.add(
        VisualizationVersion(
            visualization_id=row.id,
            version_n=1,
            spec_json=spec_json,
            author=author,
            created_at=now,
        )
    )
    db.flush()
    return row


def update_visualization(
    db: Session,
    viz_id: str,
    app_user_id: str,
    *,
    spec: dict,
    title: Optional[str] = None,
    dataset_ref: Optional[str] = None,
    author: str = "user",
    expected_version: Optional[int] = None,
) -> Optional[Visualization]:
    row = get_visualization(db, viz_id, app_user_id)
    if row is None:
        return None
    current_version = int(row.version)
    if expected_version is not None and current_version != int(expected_version):
        raise VersionConflictError(viz_id, int(expected_version), current_version)

    now = datetime.utcnow()
    spec_json = json.dumps(spec, separators=(",", ":"), default=str)
    next_version = current_version + 1
    next_title = row.title
    if title is not None:
        next_title = title
    elif spec.get("title"):
        next_title = spec["title"]
    values = {
        Visualization.spec_json: spec_json,
        Visualization.version: next_version,
        Visualization.updated_at: now,
        Visualization.title: next_title,
    }
    if dataset_ref is not None:
        values[Visualization.dataset_ref] = dataset_ref

    # Compare-and-swap makes the version check safe even when a user and agent
    # save concurrently in separate requests/processes.
    changed = (
        db.query(Visualization)
        .filter(
            Visualization.id == viz_id,
            Visualization.app_user_id == app_user_id,
            Visualization.is_deleted.is_(False),
            Visualization.version == current_version,
        )
        .update(values, synchronize_session=False)
    )
    if changed != 1:
        db.expire_all()
        latest = get_visualization(db, viz_id, app_user_id)
        latest_version = int(latest.version) if latest is not None else current_version
        raise VersionConflictError(viz_id, current_version, latest_version)

    db.add(
        VisualizationVersion(
            visualization_id=row.id,
            version_n=next_version,
            spec_json=spec_json,
            author=author,
            created_at=now,
        )
    )
    db.flush()
    db.refresh(row)
    return row


def _dataset_ref_clause(file_id: str):
    return or_(
        Visualization.dataset_ref == file_id,
        Visualization.dataset_ref.startswith(f"{file_id}::"),
    )


def count_for_dataset(db: Session, session_id: str, app_user_id: str, file_id: str) -> int:
    return (
        db.query(Visualization)
        .filter(
            Visualization.session_history_id == session_id,
            Visualization.app_user_id == app_user_id,
            Visualization.is_deleted.is_(False),
            _dataset_ref_clause(file_id),
        )
        .count()
    )


def soft_delete_for_dataset(db: Session, session_id: str, app_user_id: str, file_id: str) -> int:
    rows = (
        db.query(Visualization)
        .filter(
            Visualization.session_history_id == session_id,
            Visualization.app_user_id == app_user_id,
            Visualization.is_deleted.is_(False),
            _dataset_ref_clause(file_id),
        )
        .all()
    )
    now = datetime.utcnow()
    for row in rows:
        row.is_deleted = True
        row.updated_at = now
    if rows:
        db.flush()
    return len(rows)


def soft_delete_visualization(db: Session, viz_id: str, app_user_id: str) -> bool:
    row = get_visualization(db, viz_id, app_user_id)
    if row is None:
        return False
    row.is_deleted = True
    row.updated_at = datetime.utcnow()
    db.flush()
    return True


def list_versions(db: Session, viz_id: str, app_user_id: str) -> List[VisualizationVersion]:
    if get_visualization(db, viz_id, app_user_id) is None:
        return []
    return (
        db.query(VisualizationVersion)
        .filter(VisualizationVersion.visualization_id == viz_id)
        .order_by(VisualizationVersion.version_n.asc())
        .all()
    )


def restore_version(
    db: Session,
    viz_id: str,
    app_user_id: str,
    version_n: int,
    *,
    author: str = "user",
    expected_version: Optional[int] = None,
) -> Optional[Visualization]:
    row = get_visualization(db, viz_id, app_user_id)
    if row is None:
        return None
    target = (
        db.query(VisualizationVersion)
        .filter(
            VisualizationVersion.visualization_id == viz_id,
            VisualizationVersion.version_n == int(version_n),
        )
        .first()
    )
    if target is None:
        return None
    restored_spec = json.loads(target.spec_json)
    restored_source = str(((restored_spec.get("data") or {}).get("source") or ""))
    return update_visualization(
        db,
        viz_id,
        app_user_id,
        spec=restored_spec,
        dataset_ref=restored_source if restored_source and restored_source != "active" else None,
        author=author,
        expected_version=expected_version,
    )

"""
DB-backed store for saved preview filters and their version history.

Every write records a full-spec version row (specs are small; full copies beat
diff bookkeeping). ``restore_version`` creates a NEW head version with the old
spec so history stays linear. All reads enforce ownership via PermissionError.
Mirrors the visualization store pattern.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.models import SavedFilter, SavedFilterVersion
from database.session_store import ensure_session_row
from database.viz_store import VersionConflictError


def filter_to_dict(row: SavedFilter) -> dict:
    return {
        "id": row.id,
        "session_id": row.session_history_id,
        "dataset_ref": row.dataset_ref,
        "name": row.name,
        "spec": json.loads(row.spec_json),
        "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def version_to_dict(row: SavedFilterVersion) -> dict:
    return {
        "version": row.version_n,
        "spec": json.loads(row.spec_json),
        "author": row.author,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def get_filter(db: Session, filter_id: str, app_user_id: str) -> Optional[SavedFilter]:
    row = (
        db.query(SavedFilter)
        .filter(SavedFilter.id == filter_id, SavedFilter.is_deleted.is_(False))
        .first()
    )
    if row is None:
        return None
    if row.app_user_id != app_user_id:
        raise PermissionError("Filter belongs to a different user")
    return row


def list_filters(
    db: Session,
    session_id: str,
    app_user_id: str,
    dataset_ref: Optional[str] = None,
    dataset_ref_aliases: Optional[Sequence[str]] = None,
) -> List[SavedFilter]:
    query = db.query(SavedFilter).filter(
        SavedFilter.session_history_id == session_id,
        SavedFilter.app_user_id == app_user_id,
        SavedFilter.is_deleted.is_(False),
    )
    if dataset_ref_aliases:
        query = query.filter(SavedFilter.dataset_ref.in_(dataset_ref_aliases))
    elif dataset_ref:
        query = query.filter(SavedFilter.dataset_ref == dataset_ref)
    return query.order_by(SavedFilter.created_at.asc()).all()


def create_filter(
    db: Session,
    *,
    session_id: str,
    app_user_id: str,
    spec: dict,
    dataset_ref: str,
    name: Optional[str] = None,
    author: str = "agent",
) -> SavedFilter:
    # A filter can be created before any message is persisted — make sure the
    # session row exists so the FK holds.
    ensure_session_row(db, session_id=session_id, app_user_id=app_user_id)
    now = datetime.utcnow()
    spec_json = json.dumps(spec, separators=(",", ":"), default=str)
    row = SavedFilter(
        app_user_id=app_user_id,
        session_history_id=session_id,
        dataset_ref=dataset_ref,
        name=name or spec.get("name") or "Untitled filter",
        spec_json=spec_json,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    db.add(
        SavedFilterVersion(
            filter_id=row.id,
            version_n=1,
            spec_json=spec_json,
            author=author,
            created_at=now,
        )
    )
    db.flush()
    return row


def update_filter(
    db: Session,
    filter_id: str,
    app_user_id: str,
    *,
    spec: dict,
    name: Optional[str] = None,
    author: str = "agent",
    expected_version: Optional[int] = None,
) -> Optional[SavedFilter]:
    row = get_filter(db, filter_id, app_user_id)
    if row is None:
        return None
    current_version = int(row.version)
    if expected_version is not None and current_version != int(expected_version):
        raise VersionConflictError(filter_id, int(expected_version), current_version)

    now = datetime.utcnow()
    spec_json = json.dumps(spec, separators=(",", ":"), default=str)
    next_version = current_version + 1
    next_name = row.name
    if name is not None:
        next_name = name
    elif spec.get("name"):
        next_name = spec["name"]
    values = {
        SavedFilter.spec_json: spec_json,
        SavedFilter.version: next_version,
        SavedFilter.updated_at: now,
        SavedFilter.name: next_name,
    }

    # Compare-and-swap makes the version check safe even when a user and agent
    # save concurrently in separate requests/processes.
    changed = (
        db.query(SavedFilter)
        .filter(
            SavedFilter.id == filter_id,
            SavedFilter.app_user_id == app_user_id,
            SavedFilter.is_deleted.is_(False),
            SavedFilter.version == current_version,
        )
        .update(values, synchronize_session=False)
    )
    if changed != 1:
        db.expire_all()
        latest = get_filter(db, filter_id, app_user_id)
        latest_version = int(latest.version) if latest is not None else current_version
        raise VersionConflictError(filter_id, current_version, latest_version)

    db.add(
        SavedFilterVersion(
            filter_id=row.id,
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
        SavedFilter.dataset_ref == file_id,
        SavedFilter.dataset_ref.startswith(f"{file_id}::"),
    )


def count_for_dataset(db: Session, session_id: str, app_user_id: str, file_id: str) -> int:
    return (
        db.query(SavedFilter)
        .filter(
            SavedFilter.session_history_id == session_id,
            SavedFilter.app_user_id == app_user_id,
            SavedFilter.is_deleted.is_(False),
            _dataset_ref_clause(file_id),
        )
        .count()
    )


def soft_delete_for_dataset(db: Session, session_id: str, app_user_id: str, file_id: str) -> int:
    rows = (
        db.query(SavedFilter)
        .filter(
            SavedFilter.session_history_id == session_id,
            SavedFilter.app_user_id == app_user_id,
            SavedFilter.is_deleted.is_(False),
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


def soft_delete_filter(db: Session, filter_id: str, app_user_id: str) -> bool:
    row = get_filter(db, filter_id, app_user_id)
    if row is None:
        return False
    row.is_deleted = True
    row.updated_at = datetime.utcnow()
    db.flush()
    return True


def list_versions(db: Session, filter_id: str, app_user_id: str) -> List[SavedFilterVersion]:
    if get_filter(db, filter_id, app_user_id) is None:
        return []
    return (
        db.query(SavedFilterVersion)
        .filter(SavedFilterVersion.filter_id == filter_id)
        .order_by(SavedFilterVersion.version_n.asc())
        .all()
    )


def restore_version(
    db: Session, filter_id: str, app_user_id: str, version_n: int, *, author: str = "user"
) -> Optional[SavedFilter]:
    row = get_filter(db, filter_id, app_user_id)
    if row is None:
        return None
    target = (
        db.query(SavedFilterVersion)
        .filter(
            SavedFilterVersion.filter_id == filter_id,
            SavedFilterVersion.version_n == int(version_n),
        )
        .first()
    )
    if target is None:
        return None
    return update_filter(
        db, filter_id, app_user_id, spec=json.loads(target.spec_json), author=author
    )

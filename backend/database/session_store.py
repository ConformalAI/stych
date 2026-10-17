"""
DB-backed session history store (session_histories + session_messages).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import SessionHistory, SessionMessage


def get_session_row(db: Session, session_id: str) -> Optional[SessionHistory]:
    return db.query(SessionHistory).filter(SessionHistory.id == session_id).first()


def list_user_sessions(db: Session, app_user_id: str) -> list[SessionHistory]:
    return (
        db.query(SessionHistory)
        .filter(SessionHistory.app_user_id == app_user_id)
        .order_by(SessionHistory.last_activity_at.desc())
        .all()
    )


def ensure_session_row(
    db: Session,
    *,
    session_id: str,
    app_user_id: str,
    title: Optional[str] = None,
    is_anonymous: bool = False,
) -> SessionHistory:
    now = datetime.utcnow()
    row = get_session_row(db, session_id)
    if row is None:
        row = SessionHistory(
            id=session_id,
            app_user_id=app_user_id,
            title=title,
            started_at=now,
            last_activity_at=now,
            status="active",
            total_tokens=0,
            total_credits=0.0,
            created_at=now,
            updated_at=now,
            last_message_at=None,
            is_anonymous=is_anonymous,
        )
        db.add(row)
        db.flush()
        return row

    if row.app_user_id != app_user_id:
        raise PermissionError("Session belongs to a different user")
    row.updated_at = now
    row.last_activity_at = now
    if row.is_anonymous != is_anonymous:
        row.is_anonymous = is_anonymous
    return row


def append_message(
    db: Session,
    *,
    session_id: str,
    app_user_id: str,
    role: str,
    content: str,
    content_type: str = "text",
    token_count: Optional[int] = None,
    metadata: Optional[dict] = None,
    timestamp: Optional[datetime] = None,
) -> SessionMessage:
    row = ensure_session_row(db, session_id=session_id, app_user_id=app_user_id)
    ts = timestamp or datetime.utcnow()
    msg = SessionMessage(
        session_history_id=session_id,
        app_user_id=app_user_id,
        role=role,
        content=content,
        content_type=content_type or "text",
        metadata_json=(
            json.dumps(metadata or {}, separators=(",", ":"), default=str) if metadata else None
        ),
        token_count=token_count,
        created_at=ts,
    )
    db.add(msg)
    row.last_message_at = ts
    row.last_activity_at = ts
    row.updated_at = datetime.utcnow()
    return msg


def list_messages(
    db: Session,
    *,
    session_id: str,
    app_user_id: str,
    limit: int = 500,
) -> list[SessionMessage]:
    row = get_session_row(db, session_id)
    if row is None:
        return []
    if row.app_user_id != app_user_id:
        raise PermissionError("Session belongs to a different user")

    q = (
        db.query(SessionMessage)
        .filter(
            SessionMessage.session_history_id == session_id,
            SessionMessage.app_user_id == app_user_id,
        )
        .order_by(SessionMessage.created_at.asc())
    )
    if limit > 0:
        return q.limit(limit).all()
    return q.all()


def delete_session(db: Session, *, session_id: str, app_user_id: str) -> bool:
    row = get_session_row(db, session_id)
    if row is None:
        return False
    if row.app_user_id != app_user_id:
        raise PermissionError("Session belongs to a different user")
    # Delete child rows first — several tables have non-cascaded FKs to session_histories.
    # Omitting any of these causes an IntegrityError that silently rolls back the entire delete.
    db.query(SessionMessage).filter(SessionMessage.session_history_id == session_id).delete(
        synchronize_session=False
    )
    # Visualizations and filters (and their version history) FK session_histories
    # with nullable=False. Delete the leaf version rows before their parent
    # artifacts, then delete the session row.
    from database.models import (
        SavedFilter,
        SavedFilterVersion,
        UsageLedger,
        Visualization,
        VisualizationVersion,
    )

    viz_ids = [
        v_id
        for (v_id,) in db.query(Visualization.id).filter(
            Visualization.session_history_id == session_id
        )
    ]
    if viz_ids:
        db.query(VisualizationVersion).filter(
            VisualizationVersion.visualization_id.in_(viz_ids)
        ).delete(synchronize_session=False)
        db.query(Visualization).filter(Visualization.session_history_id == session_id).delete(
            synchronize_session=False
        )
    filter_ids = [
        filter_id
        for (filter_id,) in db.query(SavedFilter.id).filter(
            SavedFilter.session_history_id == session_id
        )
    ]
    if filter_ids:
        db.query(SavedFilterVersion).filter(SavedFilterVersion.filter_id.in_(filter_ids)).delete(
            synchronize_session=False
        )
        db.query(SavedFilter).filter(SavedFilter.session_history_id == session_id).delete(
            synchronize_session=False
        )
    # Nullify usage_ledger references — keep the billing record but unlink from the session.
    db.query(UsageLedger).filter(UsageLedger.session_history_id == session_id).update(
        {"session_history_id": None}, synchronize_session=False
    )
    db.delete(row)
    return True

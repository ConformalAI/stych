"""
DB-backed store for a user's ontology and its version history.

Mirrors ``viz_store``/``filter_store``: every write records a full-spec version
row, ``restore_version`` creates a NEW head so history stays linear, and all
reads enforce ownership via ``PermissionError``.

One difference worth knowing: an ontology is unique per user, so callers use
``get_or_create`` rather than a create/list pair. The agent's proposal flow
leans on ``proposed_names`` — rejecting a proposal is just restoring its parent
version, which is why proposals are recorded rather than staged elsewhere.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from database.models import Ontology, OntologyVersion


class VersionConflictError(RuntimeError):
    """The ontology changed after the caller began editing it."""

    def __init__(self, ontology_id: str, expected: int, current: int):
        self.ontology_id = ontology_id
        self.expected = int(expected)
        self.current = int(current)
        super().__init__(
            f"Ontology changed from version {self.expected} to {self.current}. "
            "Reload it before saving again."
        )


def ontology_to_dict(row: Ontology) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "spec": json.loads(row.spec_json),
        "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def version_to_dict(row: OntologyVersion) -> dict:
    return {
        "version": row.version_n,
        "spec": json.loads(row.spec_json),
        "author": row.author,
        "summary": row.summary,
        "proposed": json.loads(row.proposed_json) if row.proposed_json else [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _empty_spec() -> dict:
    return {
        "schemaVersion": 1,
        "entities": [],
        "relationships": [],
        "dimensions": [],
        "metrics": [],
    }


def _record_version(
    db: Session,
    row: Ontology,
    *,
    author: str,
    summary: Optional[str],
    proposed: Optional[List[str]],
) -> None:
    db.add(
        OntologyVersion(
            ontology_id=row.id,
            version_n=row.version,
            spec_json=row.spec_json,
            author=author if author in ("user", "agent") else "user",
            summary=summary,
            proposed_json=json.dumps(proposed) if proposed else None,
        )
    )


def get_ontology(db: Session, ontology_id: str, app_user_id: str) -> Optional[Ontology]:
    row = (
        db.query(Ontology)
        .filter(Ontology.id == ontology_id, Ontology.is_deleted.is_(False))
        .first()
    )
    if row is None:
        return None
    if row.app_user_id != app_user_id:
        raise PermissionError("Ontology belongs to a different user")
    return row


def get_for_user(db: Session, app_user_id: str) -> Optional[Ontology]:
    return (
        db.query(Ontology)
        .filter(
            Ontology.app_user_id == app_user_id,
            Ontology.is_deleted.is_(False),
        )
        .first()
    )


def get_or_create(db: Session, app_user_id: str) -> Ontology:
    """The user's ontology, seeded empty on first access."""
    existing = get_for_user(db, app_user_id)
    if existing is not None:
        return existing

    row = Ontology(
        app_user_id=app_user_id,
        spec_json=json.dumps(_empty_spec()),
        version=1,
    )
    db.add(row)
    db.flush()
    _record_version(db, row, author="user", summary="created", proposed=None)
    db.commit()
    db.refresh(row)
    return row


def save_spec(
    db: Session,
    ontology_id: str,
    app_user_id: str,
    spec: dict,
    *,
    expected_version: Optional[int] = None,
    title: Optional[str] = None,
    author: str = "user",
    summary: Optional[str] = None,
    proposed: Optional[List[str]] = None,
) -> Ontology:
    """Compare-and-swap write. Raises VersionConflictError on a stale base."""
    row = get_ontology(db, ontology_id, app_user_id)
    if row is None:
        raise ValueError("Ontology not found")

    current = int(row.version or 1)
    if expected_version is not None and int(expected_version) != current:
        raise VersionConflictError(ontology_id, expected_version, current)

    updated = (
        db.query(Ontology)
        .filter(Ontology.id == ontology_id, Ontology.version == current)
        .update(
            {
                "spec_json": json.dumps(spec),
                "title": title if title is not None else row.title,
                "version": current + 1,
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    if updated == 0:
        db.rollback()
        fresh = get_ontology(db, ontology_id, app_user_id)
        raise VersionConflictError(ontology_id, current, int(fresh.version) if fresh else current)

    db.flush()
    db.refresh(row)
    _record_version(db, row, author=author, summary=summary, proposed=proposed)
    db.commit()
    db.refresh(row)
    return row


def list_versions(
    db: Session, ontology_id: str, app_user_id: str, limit: int = 50
) -> List[OntologyVersion]:
    if get_ontology(db, ontology_id, app_user_id) is None:
        raise ValueError("Ontology not found")
    return (
        db.query(OntologyVersion)
        .filter(OntologyVersion.ontology_id == ontology_id)
        .order_by(OntologyVersion.version_n.desc())
        .limit(limit)
        .all()
    )


def get_version(
    db: Session, ontology_id: str, app_user_id: str, version_n: int
) -> Optional[OntologyVersion]:
    if get_ontology(db, ontology_id, app_user_id) is None:
        raise ValueError("Ontology not found")
    return (
        db.query(OntologyVersion)
        .filter(
            OntologyVersion.ontology_id == ontology_id,
            OntologyVersion.version_n == int(version_n),
        )
        .first()
    )


def restore_version(
    db: Session, ontology_id: str, app_user_id: str, version_n: int, author: str = "user"
) -> Ontology:
    """Re-apply an old spec as a NEW head version, keeping history linear."""
    target = get_version(db, ontology_id, app_user_id, version_n)
    if target is None:
        raise ValueError(f"Version {version_n} not found")
    return save_spec(
        db,
        ontology_id,
        app_user_id,
        json.loads(target.spec_json),
        author=author,
        summary=f"restored version {version_n}",
    )

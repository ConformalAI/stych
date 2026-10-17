"""Best-effort artifact purge after a session row is already gone.

Delete must not hydrate, snapshot, or archive: those are resume/logout paths.
The sidebar's source of truth is ``session_histories``; everything here can
finish after the HTTP response.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from datasources.registry import SOURCE_WORKING_COPY_IDS
from helpers.storage import storage_manager

logger = logging.getLogger(__name__)


def peek_in_memory_contexts(session_id: str) -> list[dict[str, Any]]:
    """Copy DataContext metadata already in this worker. Never loads from DB."""
    from agents.state import state_manager

    state = state_manager.states.get(session_id)
    if state is None:
        return []
    return [
        {"file_id": dc.file_id, "metadata": dict(dc.metadata or {})}
        for dc in state.get_all_data_contexts()
    ]


def _snapshot_manifest(session_id: str) -> Optional[dict[str, Any]]:
    raw = storage_manager.load_snapshot_object(session_id, "__manifest__.json")
    if not raw:
        return None
    try:
        manifest = json.loads(raw)
    except Exception as exc:
        logger.warning("Could not parse snapshot manifest for session %s: %s", session_id, exc)
        return None
    return manifest if isinstance(manifest, dict) else None


def snapshot_owned_by(session_id: str, user_id: str) -> bool:
    """True only when the snapshot manifest names this user. Missing user_id is fail-closed."""
    manifest = _snapshot_manifest(session_id)
    if not manifest:
        return False
    owner = str(manifest.get("user_id") or "").strip()
    return bool(owner) and owner == user_id


def load_snapshot_context_metas(session_id: str, user_id: str) -> list[dict[str, Any]]:
    """Read snapshot JSON only — never parquet — so warehouse drops still work.

    Refuses to return warehouse credentials unless ``manifest.user_id`` matches
    the caller. A missing ``user_id`` is treated as unowned (fail closed).
    """
    from agents.state import _restore_sensitive_meta

    manifest = _snapshot_manifest(session_id)
    if not manifest:
        return []
    owner = str(manifest.get("user_id") or "").strip()
    if owner != user_id:
        logger.warning(
            "Refusing snapshot meta load for session %s: manifest user %r, caller %r",
            session_id,
            owner or None,
            user_id,
        )
        return []

    file_ids: list[str] = []
    primary = manifest.get("primary_file_id")
    if primary:
        file_ids.append(primary)
    file_ids.extend(manifest.get("additional_file_ids") or [])

    contexts: list[dict[str, Any]] = []
    for file_id in file_ids:
        meta_bytes = storage_manager.load_snapshot_object(session_id, f"{file_id}.meta.json")
        if not meta_bytes:
            continue
        try:
            meta = json.loads(meta_bytes)
        except Exception as exc:
            logger.warning(
                "Could not parse snapshot meta for session %s file %s: %s",
                session_id,
                file_id,
                exc,
            )
            continue
        contexts.append(
            {
                "file_id": file_id,
                "metadata": _restore_sensitive_meta(meta.get("metadata") or {}),
            }
        )
    return contexts


def cleanup_cleaning_workspace(session_id: str, user_id: Optional[str] = None) -> None:
    try:
        from workspace.manager import WorkspaceManager

        WorkspaceManager().cleanup(session_id, user_id=user_id)
    except Exception as exc:
        logger.warning("Failed to cleanup cleaning workspace for session %s: %s", session_id, exc)


def delete_workspace_archive(session_id: str, user_id: Optional[str] = None) -> None:
    try:
        count = storage_manager.delete_workspace_archive(user_id or "", session_id)
        if count:
            logger.info(
                "Deleted %d workspace archive file(s) from storage for session %s",
                count,
                session_id,
            )
    except Exception as exc:
        logger.warning(
            "Failed to delete workspace archive from storage for session %s: %s",
            session_id,
            exc,
        )


def _purge_one_context(session_id: str, ctx: dict[str, Any]) -> None:
    from api.source_lifecycle import (
        drop_databricks_scratch_tables,
        drop_postgres_temp_table,
        drop_scratch_tables,
    )

    meta = ctx.get("metadata") or {}
    source = meta.get("source")
    file_id = ctx.get("file_id")
    if source in SOURCE_WORKING_COPY_IDS:
        if source == "postgres":
            drop_postgres_temp_table(session_id, meta)
        elif source == "databricks":
            drop_databricks_scratch_tables(session_id, meta)
        else:
            drop_scratch_tables(session_id, meta)
        return
    if file_id:
        storage_manager.delete_upload(file_id)


def _soft_delete_session_uploads(session_id: str, user_id: str) -> None:
    from database.connection import SessionLocal
    from database.models import UploadedFile

    db = SessionLocal()
    try:
        rows = (
            db.query(UploadedFile)
            .filter(
                UploadedFile.session_id == session_id,
                UploadedFile.app_user_id == user_id,
                UploadedFile.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        now = datetime.now()
        for row in rows:
            storage_manager.delete_upload(row.id)
            row.is_deleted = True
            row.deleted_at = now
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Failed to soft-delete uploads for session %s: %s", session_id, exc)
    finally:
        db.close()


def purge_session_artifacts(
    session_id: str,
    user_id: str,
    contexts: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Drop scratch tables, uploads, local workspace, archives, and snapshots."""
    from execution.duckdb import duckdb_kernel_manager

    try:
        duckdb_kernel_manager.close_session(session_id, user_id)
    except Exception as exc:
        logger.warning("Failed to close DuckDB kernel for session %s: %s", session_id, exc)

    captured = list(contexts or [])
    owned_snapshot = snapshot_owned_by(session_id, user_id)
    if not captured:
        try:
            captured = load_snapshot_context_metas(session_id, user_id)
        except Exception as exc:
            logger.warning("Snapshot meta load failed for session %s: %s", session_id, exc)

    for ctx in captured:
        try:
            _purge_one_context(session_id, ctx)
        except Exception as exc:
            logger.warning(
                "Context purge failed for session %s file %s: %s",
                session_id,
                ctx.get("file_id"),
                exc,
            )

    _soft_delete_session_uploads(session_id, user_id)
    cleanup_cleaning_workspace(session_id, user_id)
    delete_workspace_archive(session_id, user_id)

    if not owned_snapshot:
        logger.info(
            "Skipping snapshot object delete for session %s: ownership not confirmed",
            session_id,
        )
        return

    try:
        deleted = storage_manager.delete_session_snapshots(session_id)
        if deleted:
            logger.info("Deleted %d snapshot object(s) for session %s", deleted, session_id)
    except Exception as exc:
        logger.warning("Failed to delete snapshots for session %s: %s", session_id, exc)


async def purge_session_artifacts_async(
    session_id: str,
    user_id: str,
    contexts: Optional[list[dict[str, Any]]] = None,
) -> None:
    try:
        await asyncio.to_thread(purge_session_artifacts, session_id, user_id, contexts)
    except Exception as exc:
        logger.warning("Background session purge failed for %s: %s", session_id, exc)

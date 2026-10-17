"""Retire a chat dataset and cascade saved-connection delete across sessions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session as DbSession

from agents.state import AgentState, state_manager
from database import filter_store, viz_store
from database.models import UploadedFile
from database.session_store import list_user_sessions
from helpers.storage import storage_manager

logger = logging.getLogger(__name__)

SOURCE_REMOVED_STATUS = 410
SOURCE_REMOVED_CODE = "source_removed"
SOURCE_REMOVED_MESSAGE = "This dataset was removed from the chat."
SOURCE_REMOVED_DETAIL = {"code": SOURCE_REMOVED_CODE, "message": SOURCE_REMOVED_MESSAGE}

_SQL_SOURCES = frozenset(
    {"postgres", "sqlserver", "mongodb", "snowflake", "redshift", "bigquery", "databricks"}
)


def raise_source_removed() -> None:
    raise HTTPException(status_code=SOURCE_REMOVED_STATUS, detail=SOURCE_REMOVED_DETAIL)


@dataclass
class RetireResult:
    file_id: str
    viz_retired: int = 0
    filters_retired: int = 0


@dataclass
class ConnectionAttachment:
    session_id: str
    file_id: str
    filename: str
    session_title: Optional[str] = None


@dataclass
class ConnectionUsage:
    etag: str
    attachments: list[ConnectionAttachment] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    table_count: int = 0
    viz_count: int = 0
    filter_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "etag": self.etag,
            "sessions": self.sessions,
            "table_count": self.table_count,
            "viz_count": self.viz_count,
            "filter_count": self.filter_count,
        }


def drop_postgres_temp_table(
    session_id: str,
    metadata: dict,
    *,
    drop_orphans: bool = True,
    drop_filter_tables: bool = False,
) -> None:
    """Drop Stych-owned Postgres working copies; never the user's source table."""
    connection_url = metadata.get("connection_url")
    if not connection_url:
        return
    try:
        from agents.sql_agent import working_copies as wc
        from agents.sql_agent.runner import PostgresSQLRunner, evict_engine, temp_table_prefix

        runner = PostgresSQLRunner(connection_url)
        tables = {c["table"] for c in wc.list_copies(metadata)}
        if metadata.get("temp_table"):
            tables.add(metadata["temp_table"])
        if drop_orphans:
            try:
                prefix = f"stych_{temp_table_prefix(session_id)}"
                tables.update(runner.list_temp_tables_with_prefix(prefix))
            except Exception as scan_exc:  # noqa: BLE001
                logger.warning("Temp-table scan failed for session %s: %s", session_id, scan_exc)
        if drop_filter_tables:
            try:
                tables.update(runner.list_temp_tables_with_prefix("flt_"))
            except Exception as scan_exc:  # noqa: BLE001
                logger.warning("Filter-table scan failed for session %s: %s", session_id, scan_exc)
        for table in tables:
            runner.drop_temp_table(table)
        if drop_orphans:
            view_name = f"cleaned_{session_id.replace('-', '_')}"
            runner._execute(f'DROP VIEW IF EXISTS "public"."{view_name}"')
            evict_engine(connection_url)
        logger.info("Dropped %d working cop(ies) for session %s", len(tables), session_id)
    except Exception as exc:
        logger.warning("Failed to drop temp tables for session %s: %s", session_id, exc)


def drop_databricks_scratch_tables(
    session_id: str,
    metadata: dict,
    *,
    drop_orphans: bool = True,
) -> None:
    """Drop Stych-owned Databricks scratch copies; never the source table."""
    try:
        from agents.sql_agent import working_copies as wc
        from agents.sql_agent.databricks_runner import DatabricksSQLRunner
        from agents.sql_agent.runner import temp_table_prefix

        runner = DatabricksSQLRunner.from_meta(metadata)
        tables = {c["table"] for c in wc.list_copies(metadata)}
        if metadata.get("temp_table"):
            tables.add(metadata["temp_table"])
        if drop_orphans:
            try:
                prefix = f"stych_{temp_table_prefix(session_id)}"
                tables.update(runner.list_temp_tables_with_prefix(prefix))
            except Exception as scan_exc:  # noqa: BLE001
                logger.warning(
                    "Databricks scratch scan failed for session %s: %s", session_id, scan_exc
                )
        for table in tables:
            runner.drop_temp_table(table)
        logger.info(
            "Dropped %d Databricks scratch cop(ies) for session %s", len(tables), session_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to drop Databricks scratch tables for session %s: %s", session_id, exc
        )


def _delete_dataset_workspace_files(session_id: str, user: str, stem: str, storage) -> None:
    try:
        from workspace.manager import WorkspaceManager, dataset_stem, list_workspace_artifacts

        workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
        for art in list_workspace_artifacts(workspace):
            if dataset_stem(art["name"]) != stem:
                continue
            try:
                (workspace / art["path"]).unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to unlink workspace file %s: %s", art["path"], exc)
            try:
                storage.delete_workspace_file(user, session_id, art["name"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to delete archived workspace file %s: %s", art["name"], exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete dataset workspace files for %s: %s", session_id, exc)


def drop_scratch_tables(session_id: str, meta: dict, *, drop_orphans: bool = True) -> None:
    """Drop every Stych-owned scratch object for a session, on any engine.

    SQL connectors go through the shared runner factory; MongoDB uses its native
    scratch runner. Leaving these objects behind means full customer datasets
    sitting in scratch indefinitely.
    """
    from agents.sql_agent import working_copies as wc
    from agents.sql_agent.protocols import runner_for, supports_source_execution
    from agents.sql_agent.runner import temp_table_prefix

    try:
        if meta.get("source") == "mongodb":
            from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415

            runner = MongoScratchRunner.from_meta(meta)
        else:
            if not supports_source_execution(meta):
                return
            runner = runner_for(meta)
    except Exception as exc:  # noqa: BLE001 — no credentials means nothing to drop
        logger.info("No scratch runner for session %s cleanup: %s", session_id, exc)
        return

    tables = {c["table"] for c in wc.list_copies(meta)}
    if meta.get("temp_table"):
        tables.add(meta["temp_table"])
    if drop_orphans:
        prefixes = [f"stych_{temp_table_prefix(session_id)}", "flt_"]
        if meta.get("source") == "mongodb":
            from filters.annotate_mongo import session_collection_prefix  # noqa: PLC0415

            prefixes = [
                f"stych_{temp_table_prefix(session_id)}",
                session_collection_prefix(session_id),
            ]
        for prefix in prefixes:
            try:
                tables.update(runner.list_temp_tables_with_prefix(prefix))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Scratch scan (%s) failed for %s: %s", prefix, session_id, exc)
    dropped = 0
    for table in tables:
        try:
            runner.drop_temp_table(table)
            dropped += 1
        except Exception as exc:  # noqa: BLE001 — best-effort per table
            logger.warning("Could not drop scratch table %s: %s", table, exc)
    logger.info("Dropped %d scratch object(s) for session %s", dropped, session_id)


def _drop_scratch_for_dataset(
    session, session_id: str, file_id: str, source: str, meta: dict
) -> None:
    others = [
        c
        for c in session.get_all_data_contexts()
        if c.file_id != file_id and (c.metadata or {}).get("source") == source
    ]
    drop_orphans = not others
    if source == "postgres":
        drop_postgres_temp_table(
            session_id, meta, drop_orphans=drop_orphans, drop_filter_tables=drop_orphans
        )
    elif source == "databricks":
        drop_databricks_scratch_tables(session_id, meta, drop_orphans=drop_orphans)
    else:
        drop_scratch_tables(session_id, meta, drop_orphans=drop_orphans)


def _evict_runtime_caches(session_id: str, user: str, empty: bool) -> None:
    try:
        from datasources.adapters.base import clear_caches

        clear_caches()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to clear adapter caches: %s", exc)
    try:
        from api.diff_service import clear_diff_cache

        clear_diff_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to clear diff cache: %s", exc)
    if empty:
        try:
            from execution.duckdb import duckdb_kernel_manager

            duckdb_kernel_manager.close_session(session_id, user)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to close DuckDB kernel for session %s: %s", session_id, exc)


def retire_dataset(
    session,
    session_id: str,
    user: str,
    file_id: str,
    db: DbSession,
    *,
    storage=None,
) -> Optional[RetireResult]:
    """Remove a dataset from this session. Never drops the customer's source table."""
    from workspace.manager import dataset_stem

    storage = storage or storage_manager
    ctx = next((c for c in session.get_all_data_contexts() if c.file_id == file_id), None)
    if ctx is None:
        return None
    meta = ctx.metadata or {}
    stem = dataset_stem(ctx.filename)
    source = meta.get("source")

    _drop_scratch_for_dataset(session, session_id, file_id, source, meta)
    session.remove_data_context(file_id)
    _delete_dataset_workspace_files(session_id, user, stem, storage)

    for name in (f"{file_id}.parquet", f"{file_id}.raw.parquet", f"{file_id}.meta.json"):
        try:
            storage.delete_snapshot_object(session_id, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete snapshot %s for session %s: %s", name, session_id, exc)

    if source not in _SQL_SOURCES:
        try:
            storage.delete_upload(file_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete upload %s: %s", file_id, exc)
        row = (
            db.query(UploadedFile)
            .filter(
                UploadedFile.id == file_id,
                UploadedFile.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if row:
            row.is_deleted = True
            row.deleted_at = datetime.now()

    viz_retired = 0
    filters_retired = 0
    try:
        viz_retired = viz_store.soft_delete_for_dataset(db, session_id, user, file_id)
        filters_retired = filter_store.soft_delete_for_dataset(db, session_id, user, file_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to retire viz/filters for %s: %s", file_id, exc)

    active = session.metadata.get("active_dataset")
    if active and dataset_stem(active) == stem:
        if session.data_context:
            session.metadata["active_dataset"] = dataset_stem(session.data_context.filename)
        else:
            session.metadata.pop("active_dataset", None)

    _evict_runtime_caches(session_id, user, empty=session.dataset_count == 0)
    return RetireResult(file_id=file_id, viz_retired=viz_retired, filters_retired=filters_retired)


def _snapshot_attachments(
    session_id: str, connection_id: str, storage
) -> list[ConnectionAttachment]:
    found: list[ConnectionAttachment] = []
    manifest_bytes = storage.load_snapshot_object(session_id, "__manifest__.json")
    if not manifest_bytes:
        return found
    try:
        manifest = json.loads(manifest_bytes)
    except Exception:
        return found
    file_ids = [manifest.get("primary_file_id")] + list(manifest.get("additional_file_ids") or [])
    for file_id in file_ids:
        if not file_id:
            continue
        meta_bytes = storage.load_snapshot_object(session_id, f"{file_id}.meta.json")
        if not meta_bytes:
            continue
        try:
            payload = json.loads(meta_bytes)
        except Exception:
            continue
        metadata = payload.get("metadata") or {}
        if metadata.get("connection_id") != connection_id:
            continue
        found.append(
            ConnectionAttachment(
                session_id=session_id,
                file_id=str(file_id),
                filename=str(payload.get("filename") or file_id),
            )
        )
    return found


def _memory_attachments(user: str, connection_id: str) -> list[ConnectionAttachment]:
    found: list[ConnectionAttachment] = []
    for session_id in state_manager.list_sessions(user):
        session = state_manager.get_session(session_id, app_user_id=user)
        if session is None:
            continue
        for dc in session.get_all_data_contexts():
            if (dc.metadata or {}).get("connection_id") != connection_id:
                continue
            found.append(
                ConnectionAttachment(
                    session_id=session_id,
                    file_id=dc.file_id,
                    filename=dc.filename,
                )
            )
    return found


def collect_connection_usage(
    db: DbSession, user: str, connection_id: str, *, storage=None
) -> ConnectionUsage:
    storage = storage or storage_manager
    by_key: dict[tuple[str, str], ConnectionAttachment] = {}
    titles: dict[str, Optional[str]] = {}
    for row in list_user_sessions(db, user):
        titles[row.id] = row.title
        for att in _snapshot_attachments(row.id, connection_id, storage):
            att.session_title = row.title
            by_key[(att.session_id, att.file_id)] = att
    for att in _memory_attachments(user, connection_id):
        att.session_title = titles.get(att.session_id)
        by_key[(att.session_id, att.file_id)] = att

    attachments = sorted(by_key.values(), key=lambda a: (a.session_id, a.file_id))
    fingerprint = "|".join(f"{a.session_id}:{a.file_id}" for a in attachments)
    etag = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]

    grouped: dict[str, list[ConnectionAttachment]] = {}
    for att in attachments:
        grouped.setdefault(att.session_id, []).append(att)

    sessions: list[dict[str, Any]] = []
    viz_total = 0
    filter_total = 0
    for session_id, rows in grouped.items():
        viz_count = 0
        filter_count = 0
        for att in rows:
            try:
                viz_count += viz_store.count_for_dataset(db, session_id, user, att.file_id)
                filter_count += filter_store.count_for_dataset(db, session_id, user, att.file_id)
            except Exception:  # noqa: BLE001
                pass
        viz_total += viz_count
        filter_total += filter_count
        sessions.append(
            {
                "session_id": session_id,
                "title": rows[0].session_title,
                "tables": [att.filename for att in rows],
                "viz_count": viz_count,
                "filter_count": filter_count,
            }
        )
    return ConnectionUsage(
        etag=etag,
        attachments=attachments,
        sessions=sessions,
        table_count=len(attachments),
        viz_count=viz_total,
        filter_count=filter_total,
    )


def _session_for_retire(session_id: str, user: str) -> Optional[AgentState]:
    existing = state_manager.get_session(session_id, app_user_id=user)
    if existing is not None:
        return existing
    state = AgentState(session_id=session_id, metadata={"user_id": user})
    try:
        state_manager._restore_snapshot_sync(state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to hydrate snapshot for session %s: %s", session_id, exc)
        return None
    if not state.get_all_data_contexts():
        return None
    state_manager.states[session_id] = state
    return state


def retire_connection_attachments(
    db: DbSession,
    user: str,
    attachments: list[ConnectionAttachment],
    *,
    storage=None,
) -> list[ConnectionAttachment]:
    storage = storage or storage_manager
    detached: list[ConnectionAttachment] = []
    for att in attachments:
        session = _session_for_retire(att.session_id, user)
        if session is None:
            continue
        result = retire_dataset(session, att.session_id, user, att.file_id, db, storage=storage)
        if result is None:
            continue
        detached.append(att)
        state_manager.mark_dirty(att.session_id)
    return detached

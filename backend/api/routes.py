"""
FastAPI routes for Stych API.
Handles file uploads and agent queries via HTTP.
"""

import ast
import asyncio
import html as _html
import io
import json
import logging
import os
import time
import time as _time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request as urllib_request
from urllib.parse import urlencode

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from agents.state import DataContext, state_manager
from api.dependencies import (
    get_config,
    get_current_admin_user,
    get_current_user,
    get_current_user_record,
    get_orchestrator,
    resolve_session,
)
from api.schemas import (
    DeleteSourceRequest,
    HealthResponse,
    IntegrationCredentialRequest,
    IntegrationStatus,
    IntegrationStatusResponse,
    MemoryDeleteResponse,
    MemoryEntriesResponse,
    MemoryEntryDeleteResponse,
    MemoryEntryItem,
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryGraphResponse,
    MemoryPreferenceRequest,
    MemoryPreferenceResponse,
    MemoryStatusResponse,
    MemoryTimeseriesBucket,
    MemoryTimeseriesResponse,
    SessionInfo,
    UploadResponse,
)
from api.session_cleanup import peek_in_memory_contexts, purge_session_artifacts_async
from api.source_lifecycle import (
    retire_dataset,
)
from database.app_user_store import (
    update_long_term_memory_enabled,
    update_preferred_model,
)
from database.connection import APP_DB_SCHEMA, engine, get_db
from database.models import AppUser, UploadedFile
from helpers.helper import (
    generate_file_id,
    get_data_summary,
    read_tabular_file_from_bytes,
)
from helpers.oauth_state_store import consume_oauth_state, create_oauth_state
from helpers.sse import (
    safe_error_message,
)
from helpers.storage import storage_manager
from helpers.validators import (
    ValidationError,
    validate_csv_readable_from_bytes,
    validate_file_extension,
    validate_file_size,
)
from llm.langfuse_client import get_langfuse_tracker
from workspace.manager import dataset_parquet_name

logger = logging.getLogger(__name__)

_POST_COMMIT_SNAPSHOT_RETRY_DELAYS = (0, 1, 5)


def _session_info_extras(session) -> dict:
    """Shared SessionInfo fields derived from in-memory agent state."""
    return {
        "is_anonymous": bool((session.metadata or {}).get("is_anonymous")),
        "persist_warning": bool((session.metadata or {}).get("persist_failed")),
    }


def _sync_user_memory_preference(session, user_record: AppUser) -> None:
    """Copy the user's long-term memory preference into session metadata."""
    if session.metadata is None:
        session.metadata = {}
    session.metadata["long_term_memory_enabled"] = bool(
        getattr(user_record, "long_term_memory_enabled", True)
    )


# Create router
router = APIRouter(prefix="/api", tags=["stych"])


async def _repair_source_snapshot(session) -> None:
    """Synchronize a committed source deletion without turning storage outages into 500s."""
    for attempt, delay in enumerate(_POST_COMMIT_SNAPSHOT_RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            if session.get_all_data_contexts():
                await state_manager.snapshot_data_async(session, raise_on_error=True)
            else:
                await asyncio.to_thread(
                    storage_manager.delete_session_snapshots, session.session_id
                )
            state_manager.mark_snapshot_clean(session.session_id)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Post-delete snapshot repair attempt %d/%d failed for session %s: %s",
                attempt,
                len(_POST_COMMIT_SNAPSHOT_RETRY_DELAYS),
                session.session_id,
                exc,
            )

    # A later periodic flush will retry this repair until storage recovers.
    if session.get_all_data_contexts():
        state_manager.mark_dirty(session.session_id)
    else:
        state_manager.mark_snapshot_cleanup(session.session_id)


def _derive_requirements_from_code(code: str) -> list[str]:
    """Derive third-party requirements from import statements."""
    import sys

    requirements: list[str] = []
    seen: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None

    stdlib = set(getattr(sys, "stdlib_module_names", []))
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root and root not in stdlib:
                        if root not in seen:
                            requirements.append(root)
                            seen.add(root)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root and root not in stdlib:
                        if root not in seen:
                            requirements.append(root)
                            seen.add(root)

    # Ensure common data deps are present if referenced.
    code_lower = code.lower()
    for dep in ("pandas", "numpy"):
        if dep in code_lower and dep not in seen:
            requirements.append(dep)
            seen.add(dep)

    return requirements


@router.get("/health", response_model=HealthResponse)
async def health_check(config=Depends(get_config)):
    """Health check endpoint."""
    import time

    _start = getattr(health_check, "start_time", None)
    uptime = time.time() - (_start if isinstance(_start, float) else time.time())
    app_db_ok = True
    app_db_schema_version = None
    supertokens_ok = True

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            row = conn.execute(
                text(
                    f'SELECT version_num FROM "{APP_DB_SCHEMA}"."alembic_version" '
                    "ORDER BY version_num DESC LIMIT 1"
                )
            ).first()
            app_db_schema_version = row[0] if row else None
    except Exception:
        app_db_ok = False

    supertokens_uri = (
        config.get("supertokens", {}).get("connection_uri") or "http://localhost:3567"
    ).rstrip("/")
    try:

        def _ping_supertokens() -> bool:
            with urllib_request.urlopen(f"{supertokens_uri}/hello", timeout=2.0) as resp:
                return 200 <= int(getattr(resp, "status", 0)) < 300

        supertokens_ok = await asyncio.to_thread(_ping_supertokens)
    except Exception:
        supertokens_ok = False

    # Hindsight long-term memory probe (only when configured)
    lt_cfg = config.get("memory", {}).get("long_term", {})
    hindsight_ok = True
    hindsight_required = str(lt_cfg.get("enabled", "false")).lower() in (
        "true",
        "1",
        "yes",
    )
    if hindsight_required:
        hindsight_url = (
            lt_cfg.get("hindsight", {}).get("base_url") or "http://localhost:8888"
        ).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{hindsight_url}/health")
                hindsight_ok = resp.status_code == 200
        except Exception:
            hindsight_ok = False

    if not app_db_ok:
        overall_status = "unhealthy"
    elif not supertokens_ok or (hindsight_required and not hindsight_ok):
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return HealthResponse(
        status=overall_status,
        version="1.0.0",
        uptime_seconds=uptime,
        agents_ready=True,
        a2a_enabled=config.get("a2a", {}).get("enabled", False),
        app_db_ok=app_db_ok,
        app_db_schema_version=app_db_schema_version,
        supertokens_ok=supertokens_ok,
        hindsight_ok=hindsight_ok,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_csv(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    user: str = Depends(get_current_user),
    app_user=Depends(get_current_user_record),
    config=Depends(get_config),
    db: DbSession = Depends(get_db),
):
    """
    Upload a data file for analysis.

    - **file**: CSV, Excel, or JSON file to upload
    - **session_id**: Optional session ID (will create new if not provided)

    Multiple files can be uploaded to the same session. The first file
    becomes the primary dataset; subsequent files are stored as
    additional datasets for linking / joining operations.

    Files are persisted to the configured storage backend (local or S3/MinIO).
    File metadata is recorded in the uploaded_files table in Postgres.
    """
    file_id: Optional[str] = None
    try:
        allowed_extensions = config.get("upload", {}).get("allowed_extensions", [".csv"])
        if not file.filename:
            raise HTTPException(status_code=400, detail="Uploaded file must have a filename.")
        validate_file_extension(file.filename, allowed_extensions)

        max_size_mb = float(config.get("upload", {}).get("max_size_mb", 50))
        max_size_bytes = int(max_size_mb * 1024 * 1024)

        # Stream in 64 KB chunks so oversized files are rejected before they
        # are fully buffered into server memory.
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_size_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds the {max_size_mb:.0f} MB upload limit",
                )
            chunks.append(chunk)
        content = b"".join(chunks)

        validate_file_size(len(content), max_size_mb)

        # Validate and parse from bytes — works for both local and S3 backends
        # without requiring a local file path.
        is_valid, error = validate_csv_readable_from_bytes(content, file.filename)
        if not is_valid:
            raise HTTPException(status_code=400, detail=error)

        df, parse_error = read_tabular_file_from_bytes(content, file.filename)
        if df is None:
            raise HTTPException(status_code=400, detail=parse_error)

        summary = get_data_summary(df)

        # Resolve session_id before any DB writes so the uploaded_files row always
        # contains the correct value (the client may omit it on first upload).
        if not session_id:
            session_id = str(uuid.uuid4())

        # Persist bytes to the configured storage backend (local or S3/MinIO).
        file_id = generate_file_id()
        storage_key = storage_manager.save_upload(file_id, file.filename, content)

        # Workspace stores parquet; the UI name matches those bytes. Keep the
        # user's original name on the upload row for audit.
        display_filename = dataset_parquet_name(file.filename)

        # Record file metadata in Postgres so uploads are tracked across restarts
        # and storage backends.
        uploaded_file = UploadedFile(
            id=file_id,
            app_user_id=app_user.id,
            session_id=session_id,
            filename=display_filename,
            original_filename=file.filename,
            storage_backend=storage_manager.backend_name,
            storage_key=storage_key,
            size_bytes=len(content),
            content_type=file.content_type or "",
        )
        db.add(uploaded_file)
        db.commit()

        session = resolve_session(session_id, user)

        # Pre-compute per-column sample values so the linking agent can use
        # them directly without re-scanning the full DataFrame on every request.
        # Each value is clipped: a single wide cell (e.g. an embedded-JSON column)
        # would otherwise blow up every LLM prompt these samples are inlined into.
        _col_samples: Dict[str, Any] = {}
        for _col in df.columns[:20]:
            _col_samples[_col] = [v[:120] for v in df[_col].dropna().astype(str).unique()[:5]]

        data_context = DataContext(
            file_id=file_id,
            filename=display_filename,
            raw_dataframe=df.copy(deep=True),
            working_dataframe=df.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "user": user,
                "storage_backend": storage_manager.backend_name,
                "storage_key": storage_key,
                "column_samples": _col_samples,
            },
        )

        # Use add_data_context instead of set_data_context so that
        # multiple uploads accumulate in the session rather than
        # replacing each other. The first upload becomes the primary
        # data_context; subsequent uploads go to additional_data_contexts.
        session.add_data_context(data_context)

        # Seed the active dataset (<stem>.parquet, named after the upload)
        # into the session workspace so the analysis & cleaning agents read/write it
        # directly (lossless dtypes) and the agent/user see one identity for the dataset.
        # Only seed when absent so prior cleaning is preserved across re-uploads.
        try:
            from workspace.manager import (
                WorkspaceManager,
                dataset_stem,
                seed_all_datasets,
            )

            seed_uid = session.metadata.get("user_id") or user
            wroot = WorkspaceManager().get_or_create(session_id, user_id=seed_uid)
            # Keep the primary as the default working frame, but seed *every* dataset
            # (primary + additional) so sub-agents can read any of them directly —
            # active_dataset is now just the default focus, not the only file on disk.
            session.metadata.setdefault(
                "active_dataset", dataset_stem(session.data_context.filename)
            )
            seed_all_datasets(wroot, session)
        except Exception as seed_exc:  # noqa: BLE001 — seeding is best-effort
            logger.warning("[upload] workspace seed failed (session %s): %s", session_id, seed_exc)

        state_manager.mark_dirty(session_id)
        await state_manager.snapshot_data_async(session)

        tracker = get_langfuse_tracker()
        if tracker:
            tracker.set_session_id(session_id)
            tracker.set_user_id(user)

        logger.info(
            f"Uploaded file {file.filename} ({file_id}) for session {session_id} "
            f"from user: {user} via {storage_manager.backend_name} backend "
            f"(total datasets: {session.dataset_count})"
        )

        return UploadResponse(
            file_id=file_id,
            session_id=session_id,
            filename=display_filename,
            size_mb=len(content) / (1024 * 1024),
            rows=summary["rows"],
            columns=summary["columns"],
            column_names=summary["column_names"],
            summary=summary,
        )

    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading file (session {session_id}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=safe_error_message(e))


@router.get("/history/{session_id}")
async def get_conversation_history(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: str = Depends(get_current_user),
):
    """Get conversation history for a session."""
    try:
        session = state_manager.get_session(session_id, app_user_id=user)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        messages = session.get_recent_messages(limit)

        logger.info(f"Returning {len(messages)} messages for session {session_id} to user: {user}")

        return {
            "session_id": session_id,
            "message_count": len(messages),
            "messages": [
                {
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat(),
                    "metadata": msg.metadata,
                }
                for msg in messages
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting conversation history (session {session_id}): {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}", response_model=SessionInfo)
async def get_session_info(
    session_id: str,
    user: str = Depends(get_current_user),
):
    """Get session information.

    Returns a fresh/empty SessionInfo when the session ID does not exist yet
    (e.g. a UUID that was generated client-side but hasn't had any uploads or
    messages yet).  This prevents the frontend from spinning in a 404-loop when
    it passes a brand-new session ID on first load.
    """
    try:
        session = state_manager.get_session(session_id, app_user_id=user)

        if not session:
            # Session not in memory or DB — it's a fresh client-generated ID.
            # Return an empty info object so the caller knows there's no data yet.
            logger.info(
                f"Session {session_id} not found in DB — returning fresh info (user: {user})"
            )
            return SessionInfo(
                session_id=session_id,
                created_at=datetime.now(),
                message_count=0,
                has_data=False,
                dataset_info=None,
                is_anonymous=False,
                persist_warning=False,
            )

        # Serialize every dataset (primary + additional) so the client can
        # restore the full multi-file list after a refresh. dataset_info stays
        # the primary dataset for back-compat.
        datasets = [dc.get_summary() for dc in session.get_all_data_contexts()]
        dataset_info = datasets[0] if datasets else None

        logger.info(
            f"Session {session_id} info: has_data={dataset_info is not None}, "
            f"datasets={session.dataset_count}, user: {user}"
        )

        return SessionInfo(
            session_id=session_id,
            created_at=session.created_at,
            message_count=len(session.messages),
            has_data=session.data_context is not None,
            dataset_info=dataset_info,
            datasets=datasets,
            **_session_info_extras(session),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session info (session {session_id}): {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}")
async def delete_session(
    session_id: str,
    user: str = Depends(get_current_user),
):
    """Delete a session row immediately; purge storage/scratch in the background."""
    try:
        # Capture in-memory metadata only — never get_session(), which restores
        # parquet just so we can throw the session away.
        contexts = peek_in_memory_contexts(session_id)
        try:
            deleted = state_manager.delete_session(
                session_id, app_user_id=user, purge_storage=False
            )
        except PermissionError:
            raise HTTPException(
                status_code=403, detail="Session belongs to a different user"
            ) from None
        # Never-persisted client UUIDs and already-deleted rows are gone from
        # the user's point of view. Do not purge storage for those: snapshots
        # are keyed only by session_id, so a guessed UUID must not drop
        # another user's warehouse scratch or snapshot objects.
        if deleted:
            asyncio.create_task(purge_session_artifacts_async(session_id, user, contexts))
            logger.info(f"Deleted session {session_id} for user: {user}")
        else:
            logger.info(
                "Delete of absent session %s treated as success for user %s", session_id, user
            )
        return {"message": "Session deleted successfully", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _archive_cleaning_workspace(session_id: str, user_id: Optional[str] = None) -> None:
    """Archive local workspace files to storage before cleanup or session end."""
    try:
        from workspace.manager import WorkspaceManager

        mgr = WorkspaceManager()
        count = await mgr.archive_to_storage(session_id, user_id, storage_manager)
        if count:
            logger.info(
                "Archived %d workspace file(s) for session %s user %s",
                count,
                session_id,
                user_id,
            )
    except Exception as exc:
        logger.warning("Failed to archive cleaning workspace for session %s: %s", session_id, exc)


def _delete_workspace_artifact(session_id: str, user: str, rel_path: str) -> None:
    """Delete a single standalone workspace file (local + archived copy)."""
    from api.cleaning_export_routes import _resolve_workspace_path
    from workspace.manager import WorkspaceManager

    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    target = _resolve_workspace_path(workspace, rel_path)  # rejects traversal
    name = target.name
    try:
        target.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to unlink workspace file %s: %s", rel_path, exc)
    try:
        storage_manager.delete_workspace_file(user, session_id, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete archived workspace file %s: %s", name, exc)


def _delete_dataset(session, session_id: str, user: str, file_id: str, db: DbSession):
    """Remove a dataset from a session everywhere except the customer's source table."""
    return retire_dataset(session, session_id, user, file_id, db, storage=storage_manager)


@router.post("/session/{session_id}/delete-source")
async def delete_source(
    session_id: str,
    request: DeleteSourceRequest,
    user: str = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """Delete a single source from a session, removing it locally and from S3/prod.

    - A ``dataset``/``sql`` source, or a workspace ``file`` that mirrors a dataset,
      removes the whole dataset (context, snapshot, workspace mirrors, upload +
      DB row, Postgres temp copies). The user's real source table is never dropped.
    - A standalone workspace ``file`` (report, engineered output) deletes just it.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # dataset/sql ids embed the file_id (e.g. "<file_id>::base"); dataset ids are
    # the bare file_id. file sources carry file_id only when they mirror a dataset.
    file_id = request.file_id or (
        request.id.split("::", 1)[0] if request.kind in ("dataset", "sql") else None
    )

    retired = None
    try:
        if file_id:
            retired = _delete_dataset(session, session_id, user, file_id, db)
            if retired is None:
                raise HTTPException(status_code=404, detail="Dataset not found in session")
        elif request.kind == "file":
            _delete_workspace_artifact(session_id, user, request.id)
        else:
            raise HTTPException(status_code=400, detail="Nothing to delete for this source")
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.error("Failed to delete source for session %s: %s", session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=safe_error_message(exc))

    # The database deletion is committed. Snapshot repair runs separately, so a
    # transient storage failure cannot make a successful deletion look failed.
    state_manager.mark_dirty(session_id)
    asyncio.create_task(_repair_source_snapshot(session))

    logger.info(
        "Deleted source (kind=%s id=%s file_id=%s) from session %s for user %s",
        request.kind,
        request.id,
        file_id,
        session_id,
        user,
    )
    payload = {
        "message": "Source deleted",
        "session_id": session_id,
        "remaining_datasets": session.dataset_count,
    }
    if retired is not None:
        payload.update(
            {
                "file_id": retired.file_id,
                "viz_retired": retired.viz_retired,
                "filters_retired": retired.filters_retired,
            }
        )
    return payload


@router.post("/session/{session_id}/archive-workspace")
async def archive_session_workspace(
    session_id: str,
    user: str = Depends(get_current_user),
):
    """
    Archive the cleaning workspace for a session to persistent storage.
    Called by the frontend on logout and before starting a new session.
    """
    try:
        await _archive_cleaning_workspace(session_id, user_id=user)
        return {"archived": True, "session_id": session_id}
    except Exception as exc:
        logger.error("Error archiving workspace for session %s: %s", session_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/export/{session_id}")
async def export_data(
    session_id: str,
    format: str = Query(default="csv", description="Export format: csv, xlsx, json, parquet"),
    user: str = Depends(get_current_user),
):
    """
    Export session data in various formats.

    - **session_id**: Session ID
    - **format**: csv | xlsx | json | parquet
    """
    logger.info(f"Export request - session: {session_id}, format: {format}, user: {user}")

    tracker = get_langfuse_tracker()
    if tracker:
        tracker.set_session_id(session_id)
        tracker.set_user_id(user)

    try:
        session = state_manager.get_session(session_id, app_user_id=user)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if not session.data_context:
            raise HTTPException(status_code=400, detail="No data to export in this session")

        # working_dataframe is kept in sync with the canonical workspace data.parquet
        # (the toolset reloads it after any script that rewrites the dataset), so it is
        # authoritative for export — no need to read a separate cleaned file.
        df = session.data_context.dataframe
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename_base = f"stych_export_{timestamp}"

        if format == "csv":
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            buf.seek(0)
            return StreamingResponse(
                iter([buf.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'},
            )

        elif format == "xlsx":
            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            buf.seek(0)
            return StreamingResponse(
                iter([buf.read()]),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{filename_base}.xlsx"'},
            )

        elif format == "json":
            json_str = df.to_json(orient="records", indent=2)
            return StreamingResponse(
                iter([json_str]),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'},
            )

        elif format == "parquet":
            buf = io.BytesIO()
            df.to_parquet(buf, index=False, engine="pyarrow")
            buf.seek(0)
            return StreamingResponse(
                iter([buf.read()]),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename_base}.parquet"'},
            )

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported format '{format}'. Supported: csv, xlsx, json, parquet",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting data (session {session_id}, format {format}): {e}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/export/cleaning-script/{session_id}")
async def export_cleaning_script(
    session_id: str,
    orchestrator=Depends(get_orchestrator),
    user: str = Depends(get_current_user),
):
    """
    Export the latest cleaning script and assets as a zip bundle.

    - **session_id**: Session ID
    """
    logger.info("Cleaning script export request - session: %s, user: %s", session_id, user)

    tracker = get_langfuse_tracker()
    if tracker:
        tracker.set_session_id(session_id)
        tracker.set_user_id(user)

    try:
        session = state_manager.get_session(session_id, app_user_id=user)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        code = session.metadata.get("last_cleaning_code")
        if not code:
            raise HTTPException(
                status_code=400,
                detail="No cleaning script available. Run a cleaning step first.",
            )

        plan = session.metadata.get("last_cleaning_plan", {})
        choices = session.metadata.get("last_cleaning_choices", {})
        dataset_summary = session.data_context.get_summary() if session.data_context else {}

        readme = await orchestrator.cleaning_agent.generate_export_readme(
            code=code,
            plan=plan,
            choices=choices,
            dataset_summary=dataset_summary,
        )
        requirements = _derive_requirements_from_code(code)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename_base = f"stych_cleaning_script_{timestamp}"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("cleaning_script.py", code)
            zf.writestr("requirements.txt", "\n".join(requirements) + "\n")
            zf.writestr("README.md", readme)
        buf.seek(0)

        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.zip"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error exporting cleaning script (session %s): %s", session_id, e)
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.get("/export/cleaning-sql/{session_id}")
async def export_cleaning_sql(
    session_id: str,
    orchestrator=Depends(get_orchestrator),
    user: str = Depends(get_current_user),
):
    """
    Export the latest SQL cleaning script as a zip bundle.

    - **session_id**: Session ID
    """
    logger.info("Cleaning SQL export request - session: %s, user: %s", session_id, user)

    tracker = get_langfuse_tracker()
    if tracker:
        tracker.set_session_id(session_id)
        tracker.set_user_id(user)

    try:
        session = state_manager.get_session(session_id, app_user_id=user)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        sql = session.metadata.get("last_cleaning_sql_applied") or session.metadata.get(
            "last_cleaning_sql"
        )
        if not sql:
            raise HTTPException(
                status_code=400,
                detail="No SQL cleaning script available. Run a SQL cleaning step first.",
            )

        plan = session.metadata.get("last_cleaning_plan", {})
        choices = session.metadata.get("last_cleaning_choices", {})
        dataset_summary = session.data_context.get_summary() if session.data_context else {}

        readme = await orchestrator.cleaning_agent.generate_export_readme_sql(
            sql=sql,
            plan=plan,
            choices=choices,
            dataset_summary=dataset_summary,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename_base = f"stych_cleaning_sql_{timestamp}"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("cleaning.sql", sql)
            zf.writestr("README.md", readme)
        buf.seek(0)

        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.zip"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error exporting cleaning SQL (session %s): %s", session_id, e)
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.post("/admin/reload")
async def reload_server(
    admin_user=Depends(get_current_admin_user),
):
    """Admin endpoint to manually reload the server."""
    import os

    logger.info(
        "Manual reload triggered by admin user_id=%s email=%s",
        admin_user.id,
        admin_user.email,
    )

    try:
        main_path = Path(__file__).resolve().parent.parent / "main.py"
        os.utime(main_path, None)
        return {
            "message": "Reload triggered (if ENABLE_RELOAD=true)",
            "note": "If auto-reload is disabled, you'll need to restart the server manually",
        }
    except Exception as e:
        logger.error(f"Error triggering reload: {e}")
        raise HTTPException(status_code=500, detail="Failed to trigger reload")


# Health check endpoint start time tracker
health_check.start_time = None


# ──────────────────────────────────────────────────────────────────────────────
# Model selection endpoints
# ──────────────────────────────────────────────────────────────────────────────


async def _fetch_litellm_models(base_url: str, api_key: Optional[str]) -> list[dict]:
    """
    Call LiteLLM's OpenAI-compatible GET /models endpoint.
    Returns a list of model dicts or raises on failure.
    """
    headers: dict = {}
    if api_key and api_key.lower() not in ("", "dummy-key", "none"):
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
        resp.raise_for_status()
        return resp.json().get("data", [])


@router.get("/models")
async def get_available_models(
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """
    Return all models available through LiteLLM.

    Fetched live from LiteLLM's /models endpoint; falls back to config.yaml on failure.
    """
    llm_cfg = config.get("llm", {})
    litellm_cfg = llm_cfg.get("litellm", {})
    base_url: str = litellm_cfg.get("base_url", "http://localhost:4000")
    api_key: Optional[str] = litellm_cfg.get("api_key")
    default_model: Optional[str] = litellm_cfg.get("default_model")
    models: list[dict] = []

    try:
        raw_models = await _fetch_litellm_models(base_url, api_key)
        seen: set[str] = set()
        for m in raw_models:
            model_id = m.get("id") or m.get("model_name")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append(
                {
                    "id": model_id,
                    "provider": "litellm",
                    "is_default": model_id == default_model,
                }
            )
        logger.info("Fetched %d models from LiteLLM at %s", len(models), base_url)
    except Exception as exc:
        logger.warning(
            "Could not fetch models from LiteLLM (%s): %s — falling back to config",
            base_url,
            exc,
        )
        fallbacks: list[str] = litellm_cfg.get("fallback_models") or []
        for m in [default_model, *fallbacks]:
            if m:
                models.append({"id": m, "provider": "litellm", "is_default": m == default_model})

    return {
        "active_provider": "litellm",
        "models": models,
        "preferred_model": user_record.preferred_model,
    }


@router.get("/models/health")
async def check_router_health(
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """
    Send a minimal test inference through LiteLLM to verify the full pipeline works.
    Returns healthy=True only if a real token is received back.
    """
    llm_cfg = config.get("llm", {})
    litellm_cfg = llm_cfg.get("litellm", {})
    base_url: str = litellm_cfg.get("base_url", "http://localhost:4000")
    api_key: Optional[str] = litellm_cfg.get("api_key")
    default_model: Optional[str] = litellm_cfg.get("default_model") or "gpt-3.5-turbo"

    headers: dict = {"Content-Type": "application/json"}
    if api_key and api_key.lower() not in ("", "dummy-key", "none"):
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": default_model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 5,
    }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        reply: str = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()[:80]
        return {
            "healthy": bool(reply),
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "reply": reply or None,
        }
    except Exception as exc:
        logger.warning("LiteLLM health check failed: %s", exc)
        return {
            "healthy": False,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "reply": None,
        }


@router.get("/settings/model")
async def get_user_model(
    user_record=Depends(get_current_user_record),
):
    """Return the calling user's preferred model (or null if none set)."""
    return {"preferred_model": user_record.preferred_model}


class SetModelRequest(BaseModel):
    model_id: Optional[str] = None


@router.post("/settings/model")
async def set_user_model(
    body: SetModelRequest,
    db: DbSession = Depends(get_db),
    user_record=Depends(get_current_user_record),
):
    """Persist the user's preferred model to their app_user row."""
    updated = update_preferred_model(db, user_record.supertokens_user_id, body.model_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"preferred_model": updated.preferred_model}


# ---------------------------------------------------------------------------
# Memory settings endpoints
# ---------------------------------------------------------------------------


def _hindsight_bank_id(user_id: str, config: dict) -> str:
    """Return the Hindsight bank ID for this user."""
    prefix = (
        config.get("memory", {})
        .get("long_term", {})
        .get("hindsight", {})
        .get("bank_id_prefix", "stych")
    )
    return f"{prefix}:user:{user_id}"


def _hindsight_url(config: dict) -> str | None:
    """Return the Hindsight base URL when long-term memory is enabled, else None."""
    lt_cfg = config.get("memory", {}).get("long_term", {})
    if str(lt_cfg.get("enabled", "false")).lower() not in ("true", "1", "yes"):
        return None
    return lt_cfg.get("hindsight", {}).get("base_url", "http://localhost:8888").rstrip("/")


@router.get("/memory/status", response_model=MemoryStatusResponse)
async def get_memory_status(
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """Return long-term memory statistics for the current user's bank."""
    user_enabled = bool(getattr(user_record, "long_term_memory_enabled", True))
    base_url = _hindsight_url(config)
    if base_url is None:
        return MemoryStatusResponse(
            enabled=False,
            user_enabled=user_enabled,
            hindsight_ok=False,
            bank_exists=False,
        )

    # Must use .id (internal app-user UUID) — same ID stored in state.metadata["user_id"]
    # and used when forming bank_ids during retain/recall.
    user_id = user_record.id
    bank_id = _hindsight_bank_id(user_id, config)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/v1/default/banks/{bank_id}/stats")
    except Exception:
        return MemoryStatusResponse(
            enabled=True,
            user_enabled=user_enabled,
            hindsight_ok=False,
            bank_exists=False,
        )

    if resp.status_code == 404:
        return MemoryStatusResponse(
            enabled=True,
            user_enabled=user_enabled,
            hindsight_ok=True,
            bank_exists=False,
        )
    if resp.status_code != 200:
        return MemoryStatusResponse(
            enabled=True,
            user_enabled=user_enabled,
            hindsight_ok=False,
            bank_exists=False,
        )

    data = resp.json()
    doc_count = data.get("total_documents", 0)
    nodes = data.get("nodes_by_fact_type") or {}
    # "world" and "observation" are synthesised facts; "experience" is raw retained chunks.
    # A bank with 0 documents still exists as a shell after deletion — treat as no content.
    fact_count = nodes.get("world", 0) + nodes.get("observation", 0)
    return MemoryStatusResponse(
        enabled=True,
        user_enabled=user_enabled,
        hindsight_ok=True,
        bank_exists=doc_count > 0,  # empty shell after DELETE → false
        document_count=doc_count,
        fact_count=fact_count,
        experience_count=nodes.get("experience", 0),
        world_count=nodes.get("world", 0),
        observation_count=nodes.get("observation", 0),
        pending_operations=data.get("pending_operations", 0),
    )


@router.patch("/memory/preferences", response_model=MemoryPreferenceResponse)
async def update_memory_preferences(
    body: MemoryPreferenceRequest,
    db: DbSession = Depends(get_db),
    user_record=Depends(get_current_user_record),
):
    """Enable or disable long-term memory for the current user."""
    updated = update_long_term_memory_enabled(
        db,
        user_record.supertokens_user_id,
        body.enabled,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    return MemoryPreferenceResponse(enabled=bool(updated.long_term_memory_enabled))


@router.delete("/memory/bank", response_model=MemoryDeleteResponse)
async def delete_memory_bank(
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """Permanently delete the current user's long-term memory bank."""
    base_url = _hindsight_url(config)
    if base_url is None:
        raise HTTPException(status_code=400, detail="Long-term memory is not enabled.")

    user_id = user_record.id  # must match the ID used during retain/recall
    bank_id = _hindsight_bank_id(user_id, config)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{base_url}/v1/default/banks/{bank_id}")
    except Exception as exc:
        logger.warning("[memory] delete_bank failed: %s", exc)
        raise HTTPException(status_code=503, detail="Memory server unavailable.")

    if resp.status_code == 404:
        # Bank never existed — treat as success
        return MemoryDeleteResponse(deleted=True, message="Memory bank was already empty.")
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail="Failed to delete memory bank.")

    logger.info("[memory] bank deleted for user=%s bank=%s", user_id, bank_id)
    return MemoryDeleteResponse(deleted=True, message="All memory deleted successfully.")


@router.get("/memory/entries", response_model=MemoryEntriesResponse)
async def list_memory_entries(
    search: str | None = None,
    type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """Return paginated, optionally-filtered memory entries for the current user."""
    base_url = _hindsight_url(config)
    if base_url is None:
        raise HTTPException(status_code=400, detail="Long-term memory is not enabled.")

    user_id = user_record.id
    bank_id = _hindsight_bank_id(user_id, config)

    params: dict = {"limit": limit, "offset": offset}
    if search:
        params["q"] = search  # Hindsight uses 'q' not 'search_query'
    if type:
        params["type"] = type

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base_url}/v1/default/banks/{bank_id}/memories/list", params=params
            )
    except Exception as exc:
        logger.warning("[memory] list_entries failed: %s", exc)
        raise HTTPException(status_code=503, detail="Memory server unavailable.")

    if resp.status_code == 404:
        return MemoryEntriesResponse(items=[], total=0, limit=limit, offset=offset)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to list memory entries.")

    data = resp.json()
    raw_items = data.get("items", [])
    items: list[MemoryEntryItem] = []
    for raw in raw_items:
        # Hindsight list response uses "text" (not "content"), "fact_type" (not "type"),
        # and "date" (not "timestamp"/"created_at").
        ts = raw.get("date") or raw.get("timestamp") or raw.get("created_at")
        if ts and not isinstance(ts, str):
            ts = str(ts)
        items.append(
            MemoryEntryItem(
                id=str(raw.get("id") or raw.get("memory_id") or ""),
                content=str(raw.get("text") or raw.get("content") or ""),
                type=str(raw.get("fact_type") or raw.get("type") or "experience"),
                timestamp=ts,
                tags=list(raw.get("tags") or []),
                document_id=raw.get("chunk_id") or raw.get("document_id"),
                metadata=dict(raw.get("metadata") or {}),
            )
        )
    return MemoryEntriesResponse(
        items=items,
        total=int(data.get("total", len(items))),
        limit=int(data.get("limit", limit)),
        offset=int(data.get("offset", offset)),
    )


@router.delete("/memory/entries/{document_id:path}", response_model=MemoryEntryDeleteResponse)
async def delete_memory_entry(
    document_id: str,
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """Delete a single memory entry by its document_id."""
    base_url = _hindsight_url(config)
    if base_url is None:
        raise HTTPException(status_code=400, detail="Long-term memory is not enabled.")

    user_id = user_record.id
    bank_id = _hindsight_bank_id(user_id, config)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"{base_url}/v1/default/banks/{bank_id}/documents/{document_id}"
            )
    except Exception as exc:
        logger.warning("[memory] delete_entry failed doc_id=%s: %s", document_id, exc)
        raise HTTPException(status_code=503, detail="Memory server unavailable.")

    if resp.status_code == 404:
        return MemoryEntryDeleteResponse(deleted=True, message="Entry not found (already removed).")
    if resp.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail="Failed to delete memory entry.")

    logger.info("[memory] entry deleted doc_id=%s user=%s", document_id, user_id)
    return MemoryEntryDeleteResponse(deleted=True, message="Memory entry removed.")


@router.get("/memory/timeseries", response_model=MemoryTimeseriesResponse)
async def get_memory_timeseries(
    period: str = "30d",
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """Return memory activity timeseries (for the activity chart in Settings).

    period must be one of: 1h, 12h, 1d, 7d, 30d, 90d
    """
    base_url = _hindsight_url(config)
    if base_url is None:
        return MemoryTimeseriesResponse(buckets=[])

    user_id = user_record.id
    bank_id = _hindsight_bank_id(user_id, config)

    # Validate / normalise period — only values Hindsight accepts
    valid_periods = {"1h", "12h", "1d", "7d", "30d", "90d"}
    if period not in valid_periods:
        period = "30d"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base_url}/v1/default/banks/{bank_id}/stats/memories-timeseries",
                params={"period": period},
            )
    except Exception as exc:
        logger.warning("[memory] timeseries failed: %s", exc)
        return MemoryTimeseriesResponse(buckets=[])

    if resp.status_code != 200:
        return MemoryTimeseriesResponse(buckets=[])

    data = resp.json()
    raw_buckets = data.get("buckets", data) if isinstance(data, dict) else data
    if not isinstance(raw_buckets, list):
        raw_buckets = []

    buckets = [
        MemoryTimeseriesBucket(
            time=str(b.get("time", "")),
            world=int(b.get("world") or 0),
            experience=int(b.get("experience") or 0),
            observation=int(b.get("observation") or 0),
        )
        for b in raw_buckets
    ]
    return MemoryTimeseriesResponse(buckets=buckets)


@router.get("/memory/graph", response_model=MemoryGraphResponse)
async def get_memory_graph(
    limit: int = 100,
    config=Depends(get_config),
    user_record=Depends(get_current_user_record),
):
    """Return the memory knowledge graph (nodes + edges) for the current user."""
    base_url = _hindsight_url(config)
    if base_url is None:
        return MemoryGraphResponse(nodes=[], edges=[], total_units=0)

    user_id = user_record.id
    bank_id = _hindsight_bank_id(user_id, config)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base_url}/v1/default/banks/{bank_id}/graph",
                params={"limit": limit},
            )
    except Exception as exc:
        logger.warning("[memory] graph failed: %s", exc)
        return MemoryGraphResponse(nodes=[], edges=[], total_units=0)

    if resp.status_code != 200:
        return MemoryGraphResponse(nodes=[], edges=[], total_units=0)

    data = resp.json()
    # Hindsight wraps each node/edge in a {data: {...}} envelope
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("edges", [])

    # Hindsight omits fact_type from graph node payloads but includes it on the
    # parallel `table_rows` (keyed by the same unit id). Join it back so the graph
    # can distinguish experience / world / observation instead of defaulting all
    # nodes to "experience".
    type_by_id: dict[str, str] = {
        str(r.get("id")): str(r.get("fact_type"))
        for r in data.get("table_rows", [])
        if r.get("id") and r.get("fact_type")
    }

    nodes: list[MemoryGraphNode] = []
    for rn in raw_nodes:
        d = rn.get("data", rn)  # unwrap {data: {...}} if present
        node_id = str(d.get("id") or "")
        if not node_id:
            continue
        label = str(d.get("label") or d.get("text") or "")[:80]
        text = str(d.get("text") or d.get("label") or "")
        nodes.append(
            MemoryGraphNode(
                id=node_id,
                label=label,
                text=text,
                fact_type=str(
                    type_by_id.get(node_id) or d.get("fact_type") or d.get("type") or "experience"
                ),
                tags=list(d.get("tags") or []),
                date=str(d.get("date") or d.get("created_at") or "") or None,
            )
        )

    edges: list[MemoryGraphEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for re_ in raw_edges:
        d = re_.get("data", re_)
        src = str(d.get("source") or "")
        tgt = str(d.get("target") or "")
        if not src or not tgt or src == tgt:
            continue
        # deduplicate bidirectional duplicates
        pair = (min(src, tgt), max(src, tgt))
        if pair in seen_edges:
            continue
        seen_edges.add(pair)
        edges.append(
            MemoryGraphEdge(
                source=src,
                target=tgt,
                link_type=str(d.get("linkType") or d.get("link_type") or "semantic"),
                weight=float(d.get("weight") or 1.0),
            )
        )

    return MemoryGraphResponse(
        nodes=nodes,
        edges=edges,
        total_units=int(data.get("total_units", len(nodes))),
    )


# ---------------------------------------------------------------------------
# MCP Integrations
# ---------------------------------------------------------------------------


@router.get("/integrations", response_model=list)
async def list_integrations(
    db: DbSession = Depends(get_db),
    user_record=Depends(get_current_user_record),
):
    """Return all registered MCP integrations and connection status for the current user."""
    from agents.integration.credential_store import get_credentials
    from mcp_infra.registry import MCP_REGISTRY

    result = []
    for server_id, server_def in MCP_REGISTRY.items():
        creds = get_credentials(db, user_record.id, server_id)
        connected = creds is not None  # None means no DB row → not connected
        result.append(
            IntegrationStatus(
                server_id=server_id,
                name=server_def.name,
                description=server_def.description,
                connected=connected,
                required_credentials=server_def.required_credentials,
                optional_credentials=server_def.optional_credentials,
                settings={},
            ).model_dump()
        )
    return result


@router.put("/integrations/{server_id}", response_model=IntegrationStatusResponse)
async def save_integration_credentials(
    server_id: str,
    body: IntegrationCredentialRequest,
    db: DbSession = Depends(get_db),
    user_record=Depends(get_current_user_record),
):
    """Save (or replace) credentials for an MCP integration."""
    from agents.integration.credential_store import set_credentials
    from mcp_infra.registry import MCP_REGISTRY

    server_def = MCP_REGISTRY.get(server_id)
    if server_def is None:
        raise HTTPException(status_code=404, detail=f"Unknown integration: {server_id!r}")

    # Validate required credentials are present
    missing = [k for k in server_def.required_credentials if k not in body.credentials]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required credential fields: {missing}",
        )

    # For google_sheets: validate the service account JSON is parseable
    if server_id == "google_sheets" and "service_account_json" in body.credentials:
        try:
            import json as _json

            parsed = _json.loads(body.credentials["service_account_json"])
            if parsed.get("type") != "service_account":
                raise HTTPException(
                    status_code=422,
                    detail="service_account_json must be a Google Service Account credential (type == 'service_account').",
                )
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="service_account_json must be valid JSON.",
            )

    set_credentials(db, user_record.id, server_id, body.credentials)
    logger.info("[integrations] credentials saved user=%s server=%s", user_record.id, server_id)
    return IntegrationStatusResponse(server_id=server_id, connected=True)


@router.delete("/integrations/{server_id}", response_model=IntegrationStatusResponse)
async def delete_integration_credentials(
    server_id: str,
    db: DbSession = Depends(get_db),
    user_record=Depends(get_current_user_record),
):
    """Remove credentials for an MCP integration."""
    from agents.integration.credential_store import delete_credentials
    from mcp_infra.registry import MCP_REGISTRY

    if server_id not in MCP_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown integration: {server_id!r}")

    delete_credentials(db, user_record.id, server_id)
    logger.info("[integrations] credentials deleted user=%s server=%s", user_record.id, server_id)
    return IntegrationStatusResponse(server_id=server_id, connected=False)


# ---------------------------------------------------------------------------
# Google OAuth flow (generic — supports google_sheets, google_workspace, etc.)
# ---------------------------------------------------------------------------

# Scopes requested per server_id during OAuth consent.
_OAUTH_SCOPES: Dict[str, List[str]] = {
    "google_sheets": [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "openid",
        "email",
    ],
    "google_drive": [
        "https://www.googleapis.com/auth/drive",
        "openid",
        "email",
    ],
    "google_docs": [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
        "openid",
        "email",
    ],
    "google_slides": [
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/drive",
        "openid",
        "email",
    ],
    "google_bigquery": [
        "https://www.googleapis.com/auth/bigquery.readonly",
        "openid",
        "email",
    ],
}

_OAUTH_CLOSE_HTML = """<!DOCTYPE html>
<html>
<head><title>Connecting…</title></head>
<body style="background:#0f172a;color:{color};font-family:system-ui,sans-serif;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
  <div style="text-align:center;max-width:360px">
    <p style="font-size:1.05rem;margin:0 0 .5rem">{message}</p>
    {sub}
  </div>
  <script>{script}</script>
</body>
</html>"""


def _oauth_base_uri() -> str:
    """Return the base URI used to construct OAuth redirect URIs.

    Must be the browser-visible origin (same as API_DOMAIN / WEBSITE_DOMAIN),
    not the internal backend port — Google redirects the user's browser here and
    Vite/nginx proxies /api to the FastAPI process.
    """
    base = os.environ.get("GOOGLE_OAUTH_REDIRECT_BASE_URI", "")
    if base:
        return base.rstrip("/")
    # Legacy: full callback URL or base in GOOGLE_OAUTH_REDIRECT_URI
    old = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")
    if old:
        if "/api/integrations/" in old:
            return old.split("/api/integrations/")[0]
        return old.rstrip("/")
    # Default: match SuperTokens domains (browser-visible frontend origin)
    for env_key in ("API_DOMAIN", "WEBSITE_DOMAIN"):
        domain = os.environ.get(env_key, "")
        if domain:
            return domain.rstrip("/")
    return "http://localhost:5173"


def _oauth_page(message: str, success: bool, server_id: str = "google_sheets") -> HTMLResponse:
    """Return a minimal HTML page that notifies the opener popup via postMessage."""
    origin = _oauth_base_uri()
    payload = json.dumps(
        {
            "type": "integration_oauth",
            "serverId": server_id,
            "status": "success" if success else "error",
            "message": message,
        }
    )
    target_origin = json.dumps(origin)
    script_lines = [
        "try {",
        "  if (window.opener) {",
        f"    window.opener.postMessage({payload}, {target_origin});",
        "  }",
        "} catch (e) { console.error(e); }",
    ]
    if success:
        script_lines.append("setTimeout(() => window.close(), 800);")
    else:
        script_lines.append("setTimeout(() => window.close(), 5000);")
    script = "\n".join(script_lines)

    sub = (
        "<p style='color:#94a3b8;font-size:.85rem;margin:0'>You can close this window.</p>"
        if success
        else "<p style='color:#94a3b8;font-size:.85rem;margin:0'>This window will close shortly.</p>"
    )
    color = "#4ade80" if success else "#f87171"
    return HTMLResponse(
        _OAUTH_CLOSE_HTML.format(
            message=_html.escape(message),
            color=color,
            sub=sub,
            script=script,
        )
    )


@router.get("/integrations/{server_id}/oauth/start")
async def google_oauth_start(
    server_id: str,
    db: DbSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    """Return a Google OAuth authorization URL for the given integration."""
    from agents.integration.credential_store import get_credentials

    if server_id not in _OAUTH_SCOPES:
        raise HTTPException(status_code=404, detail=f"No OAuth flow configured for '{server_id}'")

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=500, detail="GOOGLE_OAUTH_CLIENT_ID not configured")

    redirect_uri = f"{_oauth_base_uri()}/api/integrations/{server_id}/oauth/callback"

    existing = get_credentials(db, user, server_id) or {}
    has_refresh = bool(existing.get("refresh_token"))

    state = create_oauth_state(db, user, server_id, ttl_seconds=600)

    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_OAUTH_SCOPES[server_id]),
        "access_type": "offline",
        "state": state,
    }
    # Request refresh_token only on first connect; avoid wiping tokens on re-auth.
    if not has_refresh:
        params["prompt"] = "consent"
    else:
        params["prompt"] = "select_account"

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return {"auth_url": auth_url}


@router.get("/integrations/{server_id}/oauth/callback")
async def google_oauth_callback(
    server_id: str,
    code: str = "",
    state: str = "",
    error: str = "",
    db: DbSession = Depends(get_db),
):
    """Handle Google's redirect after the user approves OAuth access."""
    from agents.integration.credential_store import get_credentials, set_credentials

    if error:
        return _oauth_page(f"Authorization denied: {error}", success=False, server_id=server_id)

    if not code or not state:
        return _oauth_page(
            "Missing code or state — please try again.", success=False, server_id=server_id
        )

    state_data = consume_oauth_state(db, state)
    if not state_data:
        return _oauth_page(
            "Authorization expired — please try again.", success=False, server_id=server_id
        )

    # Verify server_id matches what was stored in the state (CSRF guard)
    if state_data.get("server_id") != server_id:
        return _oauth_page("Invalid state — please try again.", success=False, server_id=server_id)

    user_id: str = state_data["user_id"]
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_secret:
        return _oauth_page(
            "Server misconfiguration: GOOGLE_OAUTH_CLIENT_SECRET not set.",
            success=False,
            server_id=server_id,
        )

    redirect_uri = f"{_oauth_base_uri()}/api/integrations/{server_id}/oauth/callback"

    # Exchange authorization code for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code != 200:
        logger.error("[%s_oauth] token exchange failed: %s", server_id, resp.text)
        return _oauth_page(
            "Token exchange failed — please try again.", success=False, server_id=server_id
        )

    token_data = resp.json()
    expiry_ms = int((_time.time() + token_data.get("expires_in", 3600)) * 1000)

    existing = get_credentials(db, user_id, server_id) or {}
    new_refresh = token_data.get("refresh_token") or existing.get("refresh_token", "")
    if not new_refresh:
        logger.warning("[%s_oauth] no refresh_token for user=%s", server_id, user_id)

    creds = {
        "auth_mode": "oauth",
        "access_token": token_data.get("access_token", ""),
        "refresh_token": new_refresh,
        "expiry_date": expiry_ms,
        "client_id": client_id,
        "scope": token_data.get("scope", existing.get("scope", "")),
    }

    set_credentials(db, user_id, server_id, creds)
    logger.info("[%s_oauth] connected user=%s", server_id, user_id)

    # Human-readable service name for the success page
    _service_names = {
        "google_sheets": "Google Sheets",
        "google_drive": "Google Drive",
        "google_docs": "Google Docs",
        "google_slides": "Google Slides",
        "google_bigquery": "BigQuery",
    }
    display_name = _service_names.get(server_id, server_id)
    return _oauth_page(f"{display_name} connected! ✓", success=True, server_id=server_id)

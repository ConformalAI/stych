"""
Cleaning workspace export endpoints.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from agents.state import state_manager
from api.dependencies import get_current_user
from workspace.manager import WorkspaceManager, list_workspace_artifacts

router = APIRouter()


def _resolve_workspace_path(workspace: Path, rel_path: str) -> Path:
    safe_rel = Path(rel_path)
    if safe_rel.is_absolute() or ".." in safe_rel.parts:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    target = (workspace / safe_rel).resolve()
    if not str(target).startswith(str(workspace.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return target


@router.get("/export/cleaning-files/{session_id}")
async def list_cleaning_workspace_files(
    session_id: str, user: str = Depends(get_current_user)
) -> Dict[str, List[Dict[str, str]]]:
    """
    List every artifact in the session workspace — the uploaded/active dataset plus any
    files the analysis or cleaning agents created (engineered datasets, reports, etc.).
    Returns one source of truth for the file browser, reflecting what is physically present.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context:
        raise HTTPException(status_code=400, detail="No data loaded for this session")

    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    active = session.metadata.get("active_dataset")
    files: List[Dict[str, str]] = []
    for art in list_workspace_artifacts(workspace):
        files.append(
            {
                "name": art["name"],
                "path": art["path"],
                "size_bytes": str(art["size_bytes"]),
                "modified_at": art["modified_at"],
                "suffix": art["suffix"],
                "is_active_dataset": str(bool(active) and Path(art["name"]).stem == active).lower(),
            }
        )
    return {"files": files}


@router.get("/export/cleaning-files-archive/{session_id}")
async def export_cleaning_workspace_archive(session_id: str, user: str = Depends(get_current_user)):
    """
    Download every downloadable artifact in the session workspace as one zip.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context:
        raise HTTPException(status_code=400, detail="No data loaded for this session")

    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    artifacts = [
        art
        for art in list_workspace_artifacts(workspace)
        if f".{art['suffix']}" in _DOWNLOADABLE_MEDIA
    ]
    if not artifacts:
        raise HTTPException(status_code=404, detail="No downloadable files in workspace.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for art in artifacts:
            target = _resolve_workspace_path(workspace, art["path"])
            if target.is_file():
                archive.write(target, arcname=art["path"])
    buffer.seek(0)

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="workspace_{session_id[:8]}.zip"'},
    )


# Downloadable artifact types and their media types. Excludes executable/script
# extensions so only data/report files leave the workspace.
_DOWNLOADABLE_MEDIA = {
    ".csv": "text/csv",
    ".parquet": "application/octet-stream",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".tsv": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_TABLE_EXPORT_SUFFIXES = {".csv", ".parquet", ".tsv", ".xlsx"}


def _read_table_for_export(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path, engine="pyarrow")
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".tsv":
        return pd.read_csv(path, sep="\t")
    if suffix == ".xlsx":
        return pd.read_excel(path)
    raise HTTPException(status_code=400, detail="This file cannot be converted to that format.")


@router.get("/export/cleaning-file/{session_id}")
async def export_cleaning_workspace_file(
    session_id: str,
    filename: str = Query(None, description="Workspace artifact filename"),
    path: str = Query(None, description="Workspace artifact relative path"),
    format: Optional[str] = Query(
        None, description="csv or parquet; omit to download the file as stored"
    ),
    user: str = Depends(get_current_user),
):
    """
    Download a specific artifact (dataset, report, etc.) from the session workspace.

    ``format=csv`` / ``format=parquet`` converts a tabular file on the fly so CSV
    is an export option rather than a stored twin of the parquet dataset.
    """
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context:
        raise HTTPException(status_code=400, detail="No data loaded for this session")

    workspace = WorkspaceManager().get_or_create(session_id, user_id=user)
    rel = path or filename
    if not rel:
        raise HTTPException(status_code=400, detail="Missing filename.")
    target = _resolve_workspace_path(workspace, rel)
    media_type = _DOWNLOADABLE_MEDIA.get(target.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=400, detail="This file type cannot be exported.")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")

    if format:
        fmt = format.lower().strip()
        if fmt not in ("csv", "parquet"):
            raise HTTPException(status_code=400, detail="format must be csv or parquet.")
        if target.suffix.lower() not in _TABLE_EXPORT_SUFFIXES:
            raise HTTPException(
                status_code=400, detail="Only tabular files can be exported as csv or parquet."
            )
        out_name = f"{target.stem}.{fmt}"
        if (fmt == "csv" and target.suffix.lower() == ".csv") or (
            fmt == "parquet" and target.suffix.lower() == ".parquet"
        ):
            return FileResponse(path=str(target), media_type=media_type, filename=out_name)
        df = _read_table_for_export(target)
        if fmt == "csv":
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            return Response(
                content=buf.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
            )
        buf = io.BytesIO()
        df.to_parquet(buf, index=False, engine="pyarrow")
        return Response(
            content=buf.getvalue(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )

    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=target.name,
    )

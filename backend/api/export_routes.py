"""Download a connected source as CSV or Parquet.

Serves the *live relation* for connectors whose manifest declares a full-scope
export, and the loaded frame for the rest. Truncation is never silent: a table
larger than the row cap is refused with the real count until the caller opts
into a capped download.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from agents.state import state_manager
from api import export_service, preview_service
from api.dependencies import get_config, get_current_user
from datasources import adapters as table_adapters
from datasources.contracts import DataScope

logger = logging.getLogger(__name__)

router = APIRouter()

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _adapter_http_error(exc: table_adapters.AdapterError) -> HTTPException:
    if isinstance(exc, table_adapters.AdapterUnavailable):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, table_adapters.TempTableUnavailable):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, table_adapters.AdapterTimeout):
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _filename(ctx, variant: str, fmt: str) -> str:
    meta = getattr(ctx, "metadata", None) or {}
    stem = str(meta.get("table") or meta.get("source_label") or ctx.filename or "export")
    if variant.startswith("copy:"):
        stem = f"{stem}_{variant[len('copy:'):]}"
    elif variant in ("temp", "working"):
        stem = f"{stem}_working"
    safe = _UNSAFE_FILENAME.sub("_", stem).strip("_") or "export"
    return f"{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"


def _row_count(ctx, variant: str) -> Optional[table_adapters.CountResult]:
    """Exact-or-estimated size of the relation, or None when it cannot be read."""
    try:
        return table_adapters.resolve_adapter(ctx, variant).count()
    except table_adapters.AdapterError as exc:
        logger.info("Export pre-count unavailable: %s", exc)
        return None


@router.get("/export/source/{session_id}")
def export_source(
    session_id: str,
    source_id: str = Query(..., description='DB source id: "<file_id>::base|temp|copy:<name>"'),
    format: str = Query("csv", pattern="^(csv|parquet)$"),
    truncate: bool = Query(
        False,
        description="Download the first max_rows rows instead of failing on an oversized table",
    ),
    user: str = Depends(get_current_user),
    config=Depends(get_config),
):
    """Stream one connected source. Sync ``def`` so DB reads use the threadpool."""
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ctx, variant = preview_service.resolve_sql_source_id(session, source_id)

    try:
        plan = export_service.plan_export(ctx, config)
    except export_service.ExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    headers = {
        "Content-Disposition": f'attachment; filename="{_filename(ctx, variant, format)}"',
        "X-Stych-Export-Scope": plan.scope.value,
        "X-Stych-Export-Row-Cap": str(plan.row_cap),
    }

    if not plan.streamed:
        frame = ctx.dataframe
        if len(frame) > plan.row_cap and not truncate:
            raise _too_large(len(frame), plan.row_cap, approx=False)
        headers["X-Stych-Export-Truncated"] = str(len(frame) > plan.row_cap).lower()
        batches = export_service.iter_frame_batches(frame, row_cap=plan.row_cap)
        return StreamingResponse(
            export_service.encode(batches, format),
            media_type=export_service.FORMAT_MEDIA_TYPES[format],
            headers=headers,
        )

    count = _row_count(ctx, variant)
    if count is not None and count.value > plan.row_cap and not truncate:
        raise _too_large(count.value, plan.row_cap, approx=count.approx)
    headers["X-Stych-Export-Truncated"] = str(
        bool(count is not None and count.value > plan.row_cap)
    ).lower()
    if count is not None:
        headers["X-Stych-Export-Rows"] = str(count.value)

    try:
        batches = export_service.iter_source_batches(ctx, variant, row_cap=plan.row_cap)
        return StreamingResponse(
            export_service.encode(batches, format),
            media_type=export_service.FORMAT_MEDIA_TYPES[format],
            headers=headers,
        )
    except table_adapters.AdapterError as exc:
        raise _adapter_http_error(exc) from exc


def _too_large(rows: int, cap: int, *, approx: bool) -> HTTPException:
    about = "about " if approx else ""
    return HTTPException(
        status_code=413,
        detail=(
            f"This table has {about}{rows:,} rows, above the {cap:,}-row export limit. "
            f"Retry with truncate=true to download the first {cap:,} rows."
        ),
    )


@router.get("/export/source/{session_id}/info")
def export_source_info(
    session_id: str,
    source_id: str = Query(...),
    user: str = Depends(get_current_user),
    config=Depends(get_config),
):
    """What an export of this source would do — so the UI can warn before starting."""
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    ctx, variant = preview_service.resolve_sql_source_id(session, source_id)
    try:
        plan = export_service.plan_export(ctx, config)
    except export_service.ExportError as exc:
        return {"available": False, "reason": str(exc)}

    if not plan.streamed:
        rows, approx = len(ctx.dataframe), False
    else:
        count = _row_count(ctx, variant)
        rows, approx = (count.value, count.approx) if count else (None, False)
    return {
        "available": True,
        "scope": plan.scope.value,
        "full_table": plan.scope == DataScope.FULL,
        "rows": rows,
        "rows_approximate": approx,
        "row_cap": plan.row_cap,
        "exceeds_cap": bool(rows is not None and rows > plan.row_cap),
    }

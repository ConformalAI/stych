"""
Standalone API routes for CSV Agent.

Use when the session's data comes from file upload (CSV, XLSX). Cleaning and
analysis run in-memory (pandas + sandbox).
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agents.state import state_manager
from api.dependencies import get_current_user, get_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/csv", tags=["csv-agent"])


class SessionRequest(BaseModel):
    """Request that requires a session ID."""

    session_id: str


class CleanRequest(SessionRequest):
    """Request to run cleaning on the session's in-memory data."""

    operations: Optional[List[Dict[str, Any]]] = None


class AnalyzeRequest(SessionRequest):
    """Request to run analysis (sandbox code) on the session's data."""

    message: str


@router.post("/clean")
async def clean(
    request: CleanRequest,
    user: str = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
):
    """
    Run cleaning (dedup, handle missing) on the session's DataFrame.
    Requires session to have file-based data (CSV/XLSX upload).
    """
    session = state_manager.get_session(request.session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context:
        raise HTTPException(status_code=400, detail="No data in session. Upload a CSV/XLSX first.")
    if session.data_context.is_sql_datasource():
        raise HTTPException(
            status_code=400,
            detail="Session data is from a SQL source. Use /agents/sql/clean for database cleaning.",
        )
    raise HTTPException(
        status_code=410,
        detail="CSV clean endpoint is deprecated. Use the chat cleaning flow instead.",
    )


@router.post("/analyze")
async def analyze(
    request: AnalyzeRequest,
    user: str = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
):
    """
    Run analysis: generate Python code with LLM, run in sandbox, return summarized result.
    Requires session to have file-based data.
    """
    session = state_manager.get_session(request.session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context:
        raise HTTPException(status_code=400, detail="No data in session. Upload a CSV/XLSX first.")
    if session.data_context.is_sql_datasource():
        raise HTTPException(
            status_code=400,
            detail="Session data is from a SQL source. Use /agents/sql/analyze for database analysis.",
        )
    try:
        result = await orchestrator.csv_agent.run_analysis(
            session,
            question=request.message,
            note_prefix="",
            progress_emitter=None,
        )
        return {
            "response": result.get("response", ""),
            "result_data": result.get("result_data"),
            "status": result.get("status", "completed"),
            "metadata": result.get("metadata", {}),
        }
    except Exception as e:
        logger.exception("CSV analyze failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/quality")
async def quality(
    request: SessionRequest,
    user: str = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
):
    """Return quality metrics (duplicates, missing, outliers) for the session's data."""
    session = state_manager.get_session(request.session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context:
        raise HTTPException(status_code=400, detail="No data in session.")
    if session.data_context.is_sql_datasource():
        raise HTTPException(
            status_code=400,
            detail="Use SQL agent for database session quality checks.",
        )
    try:
        metrics = orchestrator.csv_agent.compute_quality_metrics(
            session.data_context.working_dataframe
        )
        return metrics
    except Exception as e:
        logger.exception("CSV quality failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health():
    """Health check for CSV agent."""
    return {"status": "healthy", "agent": "CSVAgent"}

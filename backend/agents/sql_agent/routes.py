"""
Standalone API routes for SQL Agent.

Use when the session's data comes from Postgres (or other DB). All operations
run on a temporary table; the original table is never modified.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agents.state import state_manager
from api.dependencies import get_current_user, get_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/sql", tags=["sql-agent"])


class SessionRequest(BaseModel):
    """Request that requires a session ID."""

    session_id: str


class CleanRequest(SessionRequest):
    """Request to run cleaning on the session's SQL temp table."""

    operations: Optional[List[Dict[str, Any]]] = None


class AnalyzeRequest(SessionRequest):
    """Request to run analysis (SELECT) on the session's SQL temp table."""

    message: str


@router.post("/ensure_temp")
async def ensure_temp(
    request: SessionRequest,
    user: str = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
):
    """
    Ensure the session has a temporary table (copy of source).
    Idempotent: if temp already exists, returns its name.
    """
    session = state_manager.get_session(request.session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context or not session.data_context.is_sql_datasource():
        raise HTTPException(
            status_code=400,
            detail="Session data is not from a SQL source (Postgres). Use CSV agent for file uploads.",
        )
    try:
        schema, table = orchestrator.sql_agent.ensure_temp_table(session)
        return {
            "success": True,
            "temp_schema": schema,
            "temp_table": table,
            "message": f"Temp table {schema}.{table} ready",
        }
    except Exception as e:
        logger.exception("ensure_temp failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clean")
async def clean(
    request: CleanRequest,
    user: str = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
):
    """
    Run cleaning (dedup, handle missing) on the session's SQL temp table.
    Requires session to be loaded from Postgres.
    """
    session = state_manager.get_session(request.session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context or not session.data_context.is_sql_datasource():
        raise HTTPException(
            status_code=400,
            detail="Session data is not from a SQL source. Use CSV agent for file uploads.",
        )
    try:
        result = orchestrator.sql_agent.run_cleaning_sql(session, request.operations or [])
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("SQL clean failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze")
async def analyze(
    request: AnalyzeRequest,
    user: str = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
):
    """
    Run analysis: generate a SELECT with LLM and execute on the session's temp table.
    Returns a small result set and no full-table load.
    """
    session = state_manager.get_session(request.session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.data_context or not session.data_context.is_sql_datasource():
        raise HTTPException(
            status_code=400,
            detail="Session data is not from a SQL source. Use CSV agent for file uploads.",
        )
    try:
        # Run the agentic analysis path (multi-step run_sql ReAct loop on the temp table).
        result = await orchestrator.analysis_agent.process_query(
            state=session, message=request.message
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("response", "Analysis failed"))
        data = result.get("result_data")
        meta = session.data_context.metadata if session.data_context else {}
        temp_table_name = (
            f"{meta.get('temp_schema', '')}.{meta.get('temp_table', '')}".strip(".") or None
        )
        return {
            "success": True,
            "response": result.get("response", ""),
            "data": data,
            "rows": len(data) if isinstance(data, list) else None,
            "temp_table_name": temp_table_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("SQL analyze failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health():
    """Health check for SQL agent."""
    return {"status": "healthy", "agent": "SQLAgent"}

"""
API routes for Orchestrator Agent.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agents.state import state_manager
from api.dependencies import get_current_user, get_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/orchestrator", tags=["Orchestrator Agent"])


class OrchestrationRequest(BaseModel):
    """Request model for orchestration."""

    task_type: str
    message: str
    session_id: str


@router.post("/execute")
async def execute_task(
    request: OrchestrationRequest,
    orchestrator=Depends(get_orchestrator),
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Execute a task by routing to appropriate agent.
    Uses the app-wired orchestrator (same instance as /api/query).
    """
    try:
        state = state_manager.get_or_create_session(request.session_id, app_user_id=user)
        result = await orchestrator.execute(
            task_type=request.task_type,
            message=request.message,
            state=state,
            session_id=request.session_id,
        )
        return result
    except Exception as e:
        logger.error(f"Orchestration failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """Health check endpoint for orchestrator agent."""
    return {"status": "healthy", "agent": "OrchestratorAgent"}

"""
FastAPI routes for Formatter Agent (distributed architecture support).
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/formatter", tags=["formatter-agent"])


class FormatRequest(BaseModel):
    """Request to format data."""

    raw_data: Any
    question: Optional[str] = None
    context: Optional[str] = None


class FormatResponse(BaseModel):
    """Formatted data response."""

    formatted_data: str
    success: bool


@router.post("/format", response_model=FormatResponse)
async def format_data(
    request: FormatRequest,
    user: str = Depends(get_current_user),
):
    """
    Format raw data into beautiful markdown.

    This endpoint is for distributed agent architecture where
    the formatter agent runs as a separate service.
    """
    try:
        # In distributed mode, this would instantiate its own agent
        # For now, return a message indicating it's not implemented in standalone mode
        return FormatResponse(
            formatted_data="Formatter agent endpoint - use via orchestrator",
            success=True,
        )
    except Exception as e:
        logger.error(f"Error in formatter endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))

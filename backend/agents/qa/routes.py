"""
API routes for Q&A Agent.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from .agent import QAAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/qa", tags=["Q&A Agent"])


class QARequest(BaseModel):
    """Request model for Q&A."""

    data: Dict[str, Any]
    question: str
    conversation_history: Optional[List[Dict[str, str]]] = None


@router.post("/ask")
async def ask_question(
    request: QARequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, str]:
    """
    Answer a question about the dataset.

    Args:
        request: Q&A request with data, question, and optional history

    Returns:
        Answer to the question
    """
    try:
        df = pd.DataFrame(request.data)
        llm_router = req.app.state.llm_router
        agent = QAAgent(llm_router)
        answer = await agent.answer(
            df, request.question, conversation_history=request.conversation_history
        )
        return {"answer": answer}

    except Exception as e:
        logger.error(f"Q&A failed: {e}")
        raise HTTPException(status_code=500, detail="Q&A failed")


@router.get("/health")
async def health_check():
    """Health check endpoint for Q&A agent."""
    return {"status": "healthy", "agent": "QAAgent"}

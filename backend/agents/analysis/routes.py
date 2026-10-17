"""
API routes for Data Analysis Agent.
"""

import logging
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from .agent import DataAnalysisAgent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/analysis", tags=["Analysis Agent"])


class AnalysisRequest(BaseModel):
    """Request model for data analysis."""

    data: Dict[str, Any]  # DataFrame as dict
    query: Optional[str] = None


class AnalysisResponse(BaseModel):
    """Response model for analysis results."""

    type: str
    statistics: Dict[str, Any]
    correlations: Dict[str, Any]
    missing_values: Dict[str, Any]
    insights: str


class QuestionRequest(BaseModel):
    """Request model for Q&A."""

    data: Dict[str, Any]
    question: str


@router.post("/analyze", response_model=AnalysisResponse)
async def analyze_data(
    request: AnalysisRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Analyze dataset and generate insights.

    Args:
        request: Analysis request with data and optional query

    Returns:
        Analysis results with statistics and insights
    """
    try:
        df = pd.DataFrame(request.data)
        llm_router = req.app.state.llm_router
        agent = DataAnalysisAgent(llm_router)
        result = await agent.analyze(df, query=request.query)
        return result

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Analysis failed")


@router.post("/question")
async def answer_question(
    request: QuestionRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, str]:
    """
    Answer a specific question about the dataset.

    Args:
        request: Question request with data and question

    Returns:
        Answer to the question
    """
    try:
        df = pd.DataFrame(request.data)
        llm_router = req.app.state.llm_router
        agent = DataAnalysisAgent(llm_router)
        answer = await agent.answer_question(df, request.question)
        return {"answer": answer}

    except Exception as e:
        logger.error(f"Question answering failed: {e}")
        raise HTTPException(status_code=500, detail="Question answering failed")


@router.get("/health")
async def health_check():
    """Health check endpoint for analysis agent."""
    return {"status": "healthy", "agent": "DataAnalysisAgent"}

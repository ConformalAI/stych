"""
API routes for Data Cleaning Agent.
"""

import logging
from typing import Any, Dict, List

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, validator
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from .agent import DataCleaningAgent, DataCleaningTools

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/cleaning", tags=["Cleaning Agent"])


# ---------------------------------------------------------------------------
# Request models with input validation
# ---------------------------------------------------------------------------


class QualityCheckRequest(BaseModel):
    """Request model for quality check."""

    data: Dict[str, List[Any]]  # Changed from Dict[str, Any] for stricter typing

    @validator("data")
    def validate_data(cls, v):
        if not v:
            raise ValueError("Data cannot be empty")
        # All values must be lists
        for col, vals in v.items():
            if not isinstance(vals, list):
                raise ValueError(f"Column '{col}' must be a list, got {type(vals).__name__}")
        # All lists must have equal length
        lengths = {col: len(vals) for col, vals in v.items()}
        unique_lengths = set(lengths.values())
        if len(unique_lengths) > 1:
            raise ValueError(f"All columns must have equal length. Got: {lengths}")
        # Size limit
        row_count = next(iter(lengths.values()), 0)
        if row_count > 100_000:
            raise ValueError(f"Data exceeds maximum of 100,000 rows. Got {row_count}")
        return v


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def dict_to_dataframe(data: Dict[str, List[Any]]) -> pd.DataFrame:
    """Convert validated dict to DataFrame with error handling."""
    try:
        return pd.DataFrame(data)
    except Exception as e:
        raise ValueError(f"Could not construct DataFrame: {e}")


def serialize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare agent result for JSON serialization."""
    serialized = result.copy()
    if "data" in serialized and isinstance(serialized["data"], pd.DataFrame):
        serialized["data"] = serialized["data"].to_dict(orient="records")
    # Convert tuples to lists for consistent JSON output
    if "original_shape" in serialized:
        serialized["original_shape"] = list(serialized["original_shape"])
    if "cleaned_shape" in serialized:
        serialized["cleaned_shape"] = list(serialized["cleaned_shape"])
    return serialized


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/quality")
async def check_quality(
    request: QualityCheckRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Analyze data quality issues.

    Args:
        request: Quality check request with data

    Returns:
        Quality report with issues and recommendations
    """
    try:
        df = dict_to_dataframe(request.data)

        # --- FIX: Circular import ---
        # Uses app state instead of `from main import llm_router`
        llm_router = req.app.state.llm_router
        agent = DataCleaningAgent(llm_router)

        result = await agent.analyze_quality(df)
        return serialize_result(result)

    except Exception as e:
        logger.error(f"Quality check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check(req: Request):
    """
    Meaningful health check endpoint for cleaning agent.
    Returns 503 when dependencies are unhealthy so load balancers
    and Kubernetes readiness probes can reroute traffic automatically.
    """
    health = {
        "status": "healthy",
        "agent": "DataCleaningAgent",
        "checks": {},
    }

    # --- FIX: Meaningful health check ---

    # Check LLM router availability
    try:
        llm_router = getattr(req.app.state, "llm_router", None)
        if llm_router is None:
            raise RuntimeError("llm_router is not configured")
        health["checks"]["llm_router"] = "ok"
    except Exception as e:
        health["checks"]["llm_router"] = f"failed: {str(e)}"
        health["status"] = "unhealthy"

    # Check tools availability
    try:
        DataCleaningTools()
        health["checks"]["cleaning_tools"] = "ok"
    except Exception as e:
        health["checks"]["cleaning_tools"] = f"failed: {str(e)}"
        health["status"] = "unhealthy"

    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(content=health, status_code=status_code)

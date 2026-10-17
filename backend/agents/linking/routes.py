"""
API routes for Data Linking Agent.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.framework.fastapi import verify_session

from agents.state import state_manager
from api.dependencies import get_current_user
from tools.linking_fuzzy_tools import FuzzyLinkingTools, available_strategies
from tools.linking_tools import DataLinkingTools, get_connection

from .agent import DataLinkingAgent
from .inspect import CANDIDATE_TOP_N, STORE_KEY, inspect_source_row, public_context
from .viz import VIZ_KINDS, VizError, build_viz

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/linking", tags=["Linking Agent"])


# ---------------------------------------------------------------------------
# DB config
# ---------------------------------------------------------------------------
#
# The semantic strategy needs Postgres with pgvector. Connection routing
# is handled by tools.get_connection(), which reads (in priority order):
#   1. LINKING_DATABASE_URL  — dedicated linking-agent DB (Option B-1)
#   2. APP_DATABASE_URL      — main app DB
#   3. DATABASE_URL          — legacy fallback
#
# Passing db_config={} from routes means "use whatever the env tells you".
# Callers can still pass an explicit dict to override.


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DatasetPayload(BaseModel):
    """
    Flexible dataset input.

    Accepts column-oriented dicts (like the cleaning agent), row-oriented
    list-of-dicts, or raw CSV / JSON strings.
    """

    data: Any
    format: Optional[str] = None  # "csv", "json", or None for auto-detect

    @field_validator("data")
    @classmethod
    def validate_data(cls, v):
        if v is None or v == "" or v == {} or v == []:
            raise ValueError("Data cannot be empty")
        # Column-oriented dict validation (same rules as cleaning agent)
        if isinstance(v, dict):
            for col, vals in v.items():
                if not isinstance(vals, list):
                    raise ValueError(f"Column '{col}' must be a list, got {type(vals).__name__}")
            lengths = {col: len(vals) for col, vals in v.items()}
            unique_lengths = set(lengths.values())
            if len(unique_lengths) > 1:
                raise ValueError(f"All columns must have equal length. Got: {lengths}")
            row_count = next(iter(lengths.values()), 0)
            if row_count > 100_000:
                raise ValueError(f"Data exceeds maximum of 100,000 rows. Got {row_count}")
        return v


class JoinRequest(BaseModel):
    """Request model for an explicit join."""

    left: DatasetPayload
    right: DatasetPayload
    join_type: str = "INNER"
    left_on: Optional[str] = None
    right_on: Optional[str] = None
    join_keys: Optional[List[Dict[str, str]]] = None
    selected_columns: Optional[List[str]] = None

    @field_validator("join_type")
    @classmethod
    def validate_join_type(cls, v):
        valid = {"INNER", "LEFT", "RIGHT", "FULL OUTER"}
        if v.strip().upper() not in valid:
            raise ValueError(f"join_type must be one of {valid}")
        return v.strip().upper()


class RecommendRequest(BaseModel):
    """Request model for a join recommendation."""

    left: DatasetPayload
    right: DatasetPayload


class PreviewRequest(BaseModel):
    """Request model for join quality preview."""

    left: DatasetPayload
    right: DatasetPayload
    left_on: str
    right_on: str


class FuzzyJoinRequest(BaseModel):
    """Request model for a fuzzy / similarity-based join."""

    left: DatasetPayload
    right: DatasetPayload
    left_on: str
    right_on: str
    strategy: str = "levenshtein"
    threshold: float = 0.8
    max_matches: int = 1
    join_type: str = "inner"

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v):
        valid = available_strategies()
        if v not in valid:
            raise ValueError(f"strategy must be one of {valid}")
        return v

    @field_validator("threshold")
    @classmethod
    def validate_threshold(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")
        return v

    @field_validator("join_type")
    @classmethod
    def validate_fuzzy_join_type(cls, v):
        if v.lower() not in {"inner", "left"}:
            raise ValueError("join_type must be 'inner' or 'left' for fuzzy joins")
        return v.lower()


class CompareStrategiesRequest(BaseModel):
    """Request model for comparing fuzzy strategies."""

    left: DatasetPayload
    right: DatasetPayload
    left_on: str
    right_on: str
    strategies: Optional[List[str]] = None
    threshold: float = 0.8
    sample_size: int = 100


class DatasetDescriptor(BaseModel):
    """
    Single dataset in a multi-join request.

    Supports all formats: column-oriented dict, row-oriented list-of-dicts,
    CSV string, TSV string, JSON string, file path, or base64-encoded
    Excel / Parquet.
    """

    data: Any
    name: Optional[str] = None
    format: Optional[str] = None  # csv, tsv, json, xlsx, parquet, base64

    @field_validator("data")
    @classmethod
    def validate_data(cls, v):
        if v is None or v == "" or v == {} or v == []:
            raise ValueError("Data cannot be empty")
        return v


class JoinStepDescriptor(BaseModel):
    """Single step in an explicit multi-join plan."""

    left: Any  # index (int) or name (str)
    right: Any  # index (int) or name (str)
    left_on: Optional[str] = None
    right_on: Optional[str] = None
    join_type: Optional[str] = None
    strategy: Optional[str] = None  # "exact" or any fuzzy strategy
    threshold: Optional[float] = None


class MultiJoinRequest(BaseModel):
    """Request model for joining N datasets."""

    datasets: List[DatasetDescriptor]
    join_plan: Optional[List[JoinStepDescriptor]] = None
    default_join_type: str = "INNER"
    match_strategy: str = "exact"
    fuzzy_threshold: float = 0.8

    @field_validator("datasets")
    @classmethod
    def validate_min_datasets(cls, v):
        if len(v) < 2:
            raise ValueError("At least 2 datasets are required")
        if len(v) > 20:
            raise ValueError("Maximum 20 datasets supported")
        return v


class MultiRecommendRequest(BaseModel):
    """Request model for recommending a multi-dataset join plan."""

    datasets: List[DatasetDescriptor]

    @field_validator("datasets")
    @classmethod
    def validate_min_datasets(cls, v):
        if len(v) < 2:
            raise ValueError("At least 2 datasets are required")
        if len(v) > 20:
            raise ValueError("Maximum 20 datasets supported")
        return v


class InspectRequest(BaseModel):
    """Look up top-N scored candidates for a clicked source row."""

    session_id: str
    source_row_id: int
    strategy: Optional[str] = None  # concrete name, or "semantic" / "lexical"
    threshold: Optional[float] = None
    top_n: int = CANDIDATE_TOP_N

    @field_validator("top_n")
    @classmethod
    def validate_top_n(cls, v):
        if v < 1 or v > 50:
            raise ValueError("top_n must be between 1 and 50")
        return v

    @field_validator("threshold")
    @classmethod
    def validate_inspect_threshold(cls, v):
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")
        return v


class VizRequest(BaseModel):
    """Build a Vega-Lite / Vega spec from the session candidate-score table."""

    session_id: str
    kind: str
    strategy: Optional[str] = None  # concrete name, or "semantic" / "lexical"
    threshold: Optional[float] = None
    dark: bool = False

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v):
        name = (v or "").strip().lower()
        if name not in VIZ_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(VIZ_KINDS)}")
        return name

    @field_validator("threshold")
    @classmethod
    def validate_viz_threshold(cls, v):
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")
        return v


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def serialize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare agent result for JSON serialization."""
    serialized = result.copy()
    if "data" in serialized and isinstance(serialized["data"], pd.DataFrame):
        serialized["data"] = serialized["data"].to_dict(orient="records")
    # Convert tuples to lists
    for key in ("original_shape", "cleaned_shape"):
        if key in serialized and isinstance(serialized[key], tuple):
            serialized[key] = list(serialized[key])
    # Full candidate table stays on the session; HTTP join payloads get a summary.
    inspect_store = serialized.pop("inspect", None)
    if inspect_store:
        serialized["inspect"] = public_context(inspect_store)
        serialized["candidate_count"] = len(inspect_store.get("candidates") or [])
    candidates = serialized.get("candidates")
    if isinstance(candidates, list) and len(candidates) > 200:
        serialized["candidates"] = candidates[:200]
        serialized["candidates_truncated"] = True
    return serialized


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/join")
async def join_data(
    request: JoinRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Join two datasets via SQL.

    Accepts explicit join keys or auto-detects matching column names.
    Returns the joined data, the SQL query used, quality metrics, and
    reproducible code.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        result = await agent.join_data(
            left_data=request.left.data,
            right_data=request.right.data,
            join_type=request.join_type,
            left_on=request.left_on,
            right_on=request.right_on,
            join_keys=request.join_keys,
            left_fmt=request.left.format,
            right_fmt=request.right.format,
            selected_columns=request.selected_columns,
        )

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Join failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/recommend")
async def recommend_join(
    request: RecommendRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Analyse two datasets and recommend a join strategy.

    Returns candidate key pairs, overlap / quality metrics, and an
    LLM-generated recommendation.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        result = await agent.recommend_join(
            left_data=request.left.data,
            right_data=request.right.data,
            left_fmt=request.left.format,
            right_fmt=request.right.format,
        )

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Recommend failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/preview")
async def preview_join(
    request: PreviewRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Preview join quality without executing the join.

    Returns overlap statistics, cardinality info, and predicted row
    counts for each join type.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        result = await agent.preview_join(
            left_data=request.left.data,
            right_data=request.right.data,
            left_on=request.left_on,
            right_on=request.right_on,
            left_fmt=request.left.format,
            right_fmt=request.right.format,
        )

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Preview failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fuzzy-join")
async def fuzzy_join_data(
    request: FuzzyJoinRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Join two datasets using fuzzy / similarity matching.

    Strategies: levenshtein, jaro_winkler, token_sort, token_set,
    partial_ratio, cosine_tfidf, soundex, metaphone, semantic.

    Returns the joined data, match details, and quality statistics.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        result = await agent.fuzzy_join_data(
            left_data=request.left.data,
            right_data=request.right.data,
            left_on=request.left_on,
            right_on=request.right_on,
            strategy=request.strategy,
            threshold=request.threshold,
            max_matches=request.max_matches,
            join_type=request.join_type,
            left_fmt=request.left.format,
            right_fmt=request.right.format,
        )

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Fuzzy join failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/compare-strategies")
async def compare_strategies(
    request: CompareStrategiesRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Run multiple fuzzy strategies on a sample and compare match rates.

    Returns ranked results with the recommended strategy.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        result = await agent.compare_strategies(
            left_data=request.left.data,
            right_data=request.right.data,
            left_on=request.left_on,
            right_on=request.right_on,
            strategies=request.strategies,
            threshold=request.threshold,
            sample_size=request.sample_size,
            left_fmt=request.left.format,
            right_fmt=request.right.format,
        )

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Compare strategies failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/strategies")
async def list_strategies() -> Dict[str, Any]:
    """
    List all available fuzzy matching strategies with descriptions.
    """
    descriptions = {
        "levenshtein": "Edit distance — counts insertions, deletions, substitutions. Good for typos.",
        "jaro_winkler": "Optimised for short strings like names. Boosts matches sharing a common prefix.",
        "token_sort": "Sorts words alphabetically, then compares. Handles word reordering (John Smith ↔ Smith John).",
        "token_set": "Compares word sets ignoring extra words (Apple Inc ↔ Apple Incorporated Company).",
        "partial_ratio": "Finds best substring match. Good when one value is a substring of the other.",
        "cosine_tfidf": "TF-IDF vectorisation + cosine similarity. Best for longer text like addresses or descriptions.",
        "soundex": "Phonetic code matching. Groups words that sound alike (Smith ↔ Smyth). Binary: match or no match.",
        "metaphone": "Improved phonetic matching, more accurate than Soundex for English names.",
        "semantic": "Embedding-based semantic similarity via pgvector. Matches by meaning rather than character overlap. Best for synonyms and paraphrases (Alphabet Inc ↔ Google, NYC ↔ New York City). Requires the pgvector Postgres extension and an OpenRouter API key.",
    }
    return {
        "strategies": [
            {"name": s, "description": descriptions.get(s, "")} for s in available_strategies()
        ]
    }


@router.post("/multi-join")
async def multi_join(
    request: MultiJoinRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Join N datasets together (chained or explicit plan).

    Accepts 2-20 datasets in any supported format. If no join_plan
    is provided, datasets are joined sequentially with auto-detected keys.
    Supports mixing exact and fuzzy strategies per step.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        datasets = [
            {
                "data": ds.data,
                "name": ds.name,
                "format": ds.format,
            }
            for ds in request.datasets
        ]

        join_plan = None
        if request.join_plan:
            join_plan = [step.model_dump() for step in request.join_plan]

        result = await agent.multi_join(
            datasets=datasets,
            join_plan=join_plan,
            default_join_type=request.default_join_type,
            match_strategy=request.match_strategy,
            fuzzy_threshold=request.fuzzy_threshold,
        )

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Multi-join failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/multi-recommend")
async def multi_recommend(
    request: MultiRecommendRequest,
    req: Request,
    _session: SessionContainer = Depends(verify_session()),
) -> Dict[str, Any]:
    """
    Analyse N datasets and recommend a join plan.

    Returns pairwise overlap analysis, fuzzy quality analysis,
    and an LLM-generated step-by-step join plan.
    """
    try:
        llm_router = req.app.state.llm_router
        agent = DataLinkingAgent(llm_router, db_config={})

        datasets = [
            {
                "data": ds.data,
                "name": ds.name,
                "format": ds.format,
            }
            for ds in request.datasets
        ]

        result = await agent.recommend_multi_join(datasets=datasets)

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return serialize_result(result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Multi-recommend failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _inspect_store_for_session(session_id: str, user: str) -> Dict[str, Any]:
    session = state_manager.get_session(session_id, app_user_id=user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    store = (session.metadata or {}).get(STORE_KEY)
    if not store:
        raise HTTPException(
            status_code=404,
            detail="No linking inspect data in this session. Link datasets first.",
        )
    return store


@router.get("/inspect/context")
async def inspect_context(
    session_id: str = Query(...),
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Return the inspect UI payload (dataset previews, join plan, available
    strategies) for the session's last linking run. Does not include the
    full candidate table — use POST /inspect for a clicked row.
    """
    store = _inspect_store_for_session(session_id, user)
    ctx = public_context(store)
    return {"context": ctx}


@router.post("/inspect")
async def inspect_link(
    request: InspectRequest,
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Ranked candidates for a source row, with scores.

    Given a clicked `source_row_id` from dataset 1, returns the top-N
    rows in dataset 2 that were considered matches, plus the strategy
    and similarity score for each.
    """
    store = _inspect_store_for_session(request.session_id, user)
    return inspect_source_row(
        store,
        request.source_row_id,
        strategy=request.strategy,
        threshold=request.threshold,
        top_n=request.top_n,
    )


@router.post("/viz")
async def linking_viz(
    request: VizRequest,
    user: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Parametrized linkage chart from the inspect candidate table.

    Kinds: score_matrix (Vega-Lite heatmap), coordinated (concat +
    selection), bipartite (Vega rule-mark edge view). Strategy and
    threshold are applied server-side so the specs stay selection-only.
    """
    store = _inspect_store_for_session(request.session_id, user)
    try:
        return build_viz(
            request.kind,
            store.get("candidates") or [],
            strategy=request.strategy,
            threshold=request.threshold,
            dark=request.dark,
        )
    except VizError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Same inspect handlers on /api/linking/* so the Vite / production /api proxy
# can reach them (the agent router lives at /agents/linking).
inspect_api_router = APIRouter(prefix="/api/linking", tags=["Linking Inspect"])
inspect_api_router.add_api_route(
    "/inspect/context", inspect_context, methods=["GET"], name="inspect_context_api"
)
inspect_api_router.add_api_route(
    "/inspect", inspect_link, methods=["POST"], name="inspect_link_api"
)
inspect_api_router.add_api_route("/viz", linking_viz, methods=["POST"], name="linking_viz_api")


@router.get("/health")
async def health_check(req: Request):
    """
    Health check for the linking agent.

    Verifies LLM router availability, Postgres connectivity, fuzzy
    dependencies, and the pgvector extension required by the semantic
    strategy.
    """
    import os

    health: Dict[str, Any] = {
        "status": "healthy",
        "agent": "DataLinkingAgent",
        "checks": {},
    }

    # Surface which env var the linking agent is using for its DB.
    # Helps debugging in B-1 setups (separate pgvector container).
    if os.getenv("LINKING_DATABASE_URL"):
        health["checks"]["db_source"] = "LINKING_DATABASE_URL"
    elif os.getenv("APP_DATABASE_URL"):
        health["checks"]["db_source"] = "APP_DATABASE_URL"
    elif os.getenv("DATABASE_URL"):
        health["checks"]["db_source"] = "DATABASE_URL"
    else:
        health["checks"]["db_source"] = "local defaults"

    # Check LLM router
    try:
        llm_router = getattr(req.app.state, "llm_router", None)
        if llm_router is None:
            raise RuntimeError("llm_router is not configured")
        health["checks"]["llm_router"] = "ok"
    except Exception as e:
        health["checks"]["llm_router"] = f"failed: {e}"
        health["status"] = "unhealthy"

    # Check Postgres connectivity. get_connection() now reads env vars itself.
    pg_conn = None
    try:
        pg_conn = get_connection({})
        with pg_conn.cursor() as cur:
            cur.execute("SELECT 1")
        health["checks"]["postgres"] = "ok"
    except Exception as e:
        health["checks"]["postgres"] = f"failed: {e}"
        health["status"] = "unhealthy"

    # Check pgvector extension (needed for the semantic strategy).
    # Don't mark overall status unhealthy if it's missing — the other
    # strategies still work. Just surface a clear warning.
    if pg_conn is not None:
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
                row = cur.fetchone()
            if row:
                health["checks"]["pgvector"] = f"ok (v{row[0]})"
            else:
                health["checks"]["pgvector"] = (
                    "not installed — semantic strategy unavailable. "
                    "Run: CREATE EXTENSION vector; (requires pgvector/pgvector image)"
                )
        except Exception as e:
            health["checks"]["pgvector"] = f"check failed: {e}"

    if pg_conn is not None:
        try:
            pg_conn.close()
        except Exception:
            pass

    # Check tools
    try:
        DataLinkingTools()
        health["checks"]["linking_tools"] = "ok"
    except Exception as e:
        health["checks"]["linking_tools"] = f"failed: {e}"
        health["status"] = "unhealthy"

    # Check fuzzy tools and dependencies
    try:
        FuzzyLinkingTools()
        health["checks"]["fuzzy_tools"] = "ok"
        health["checks"]["fuzzy_strategies"] = available_strategies()
    except Exception as e:
        health["checks"]["fuzzy_tools"] = f"failed: {e}"
        health["status"] = "unhealthy"

    # Check fuzzy + embedding dependencies via importlib
    import importlib.util

    fuzzy_deps = {}
    for pkg, label, install in [
        ("rapidfuzz", "rapidfuzz", "rapidfuzz"),
        ("jellyfish", "jellyfish", "jellyfish"),
        ("sklearn", "scikit-learn", "scikit-learn"),
        ("pgvector", "pgvector", "pgvector"),
        ("httpx", "httpx", "httpx"),
        ("numpy", "numpy", "numpy"),
    ]:
        fuzzy_deps[label] = (
            "ok" if importlib.util.find_spec(pkg) else f"missing — pip install {install}"
        )
    health["checks"]["fuzzy_dependencies"] = fuzzy_deps

    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(content=health, status_code=status_code)

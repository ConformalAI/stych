"""
Main FastAPI application for Stych backend.
"""

# ── Environment bootstrap ────────────────────────────────────────────────────
# MUST happen before any other local import.  Several modules (database/
# connection.py, auth/supertokens_config.py, llm/infer.py, …) read
# os.getenv() at module-import time, so load_dotenv() must run first.
import os
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent / ".env"

# In Docker, the runtime already injected env vars from docker-compose
# `environment` and `env_file` blocks before Python starts — those must win
# (e.g. SUPERTOKENS_CONNECTION_URI overridden to use the service name).
# Outside Docker (local dev), there is no such injection so .env must win over
# any accidentally-exported shell vars.
_in_docker = Path("/.dockerenv").exists()
load_dotenv(dotenv_path=_env_path, override=not _in_docker)
# ── End bootstrap ────────────────────────────────────────────────────────────

import logging
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agents import OrchestratorAgent
from agents.analysis.routes import router as analysis_router
from agents.cleaning.routes import router as cleaning_router
from agents.csv_agent.routes import router as csv_agent_router
from agents.formatter.routes import router as formatter_router
from agents.linking.routes import inspect_api_router
from agents.linking.routes import router as linking_router
from agents.orchestrator.routes import router as orchestrator_router
from agents.protocols import A2AConfig, StychA2AProtocol
from agents.qa.routes import router as qa_router
from agents.sql_agent.routes import router as sql_agent_router
from api.agent_run_routes import router as agent_run_router
from api.auth_routes import dev_router as auth_dev_router
from api.auth_routes import router as auth_router
from api.auth_routes import session_router
from api.cleaning_export_routes import router as cleaning_export_router
from api.cloud_datasource_routes import router as cloud_datasource_router
from api.connection_routes import router as connection_router
from api.datasource_routes import router as datasource_router
from api.dependencies import set_dependencies
from api.export_routes import router as export_router
from api.preview_routes import router as preview_router
from api.routes import health_check, router
from auth.supertokens_config import init_supertokens
from database.connection import init_db
from filters.routes import router as filter_router
from helpers.security_config import validate_security_config
from helpers.storage import init_storage
from llm.infer import LLMRouter
from llm.langfuse_client import init_langfuse_tracker
from ontology.routes import router as ontology_router
from viz.routes import router as viz_router

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

if os.environ.get("DEBUG_INTEGRATION"):
    for _debug_logger in ("agents.integration", "integrations.mcp_client"):
        logging.getLogger(_debug_logger).setLevel(logging.DEBUG)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict.

    - Dict values are merged recursively.
    - List values in *override* fully replace those in *base* (no appending).
    - All other values in *override* win over *base*.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# Load configuration
def load_config():
    """Load and merge configuration from config.yaml + routing_config.yaml."""
    config_dir = Path(__file__).parent / "config"
    config_path = config_dir / "config.yaml"

    if not config_path.exists():
        logger.warning(f"Config file not found at {config_path}, using defaults")
        return {}

    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    # Merge routing_config.yaml (if present) before env-var substitution so
    # that ${VAR} placeholders inside routing_config also get resolved.
    routing_path = config_dir / "routing_config.yaml"
    if routing_path.exists():
        try:
            with open(routing_path, "r") as f:
                routing_config = yaml.safe_load(f) or {}
            config = _deep_merge(config, routing_config)
            logger.info("Loaded routing_config.yaml and merged into main config")
        except yaml.YAMLError as exc:
            logger.error("routing_config.yaml is malformed — skipping merge: %s", exc)
    else:
        logger.warning("routing_config.yaml not found — agents will use in-code defaults")

    # Replace environment variable placeholders.
    # Format: ${VAR_NAME} or ${VAR_NAME:default}
    # We split only on the FIRST colon so that URL defaults like
    # ${LITELLM_BASE_URL:http://localhost:4000} are handled correctly.
    def replace_env_vars(obj):
        if isinstance(obj, dict):
            return {k: replace_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_env_vars(item) for item in obj]
        elif isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            inner = obj[2:-1]
            i = inner.find(":")
            env_var = inner if i < 0 else inner[:i]
            default = "" if i < 0 else inner[i + 1 :]
            return os.getenv(env_var, default)
        return obj

    return replace_env_vars(config)


# Global configuration
config = load_config()

# Initialise SuperTokens BEFORE the FastAPI app is created so the SDK can
# register its internal route handlers and middleware hooks.
init_supertokens(config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Handles startup and shutdown events.
    """
    # Startup
    logger.info("Starting Stych backend...")
    validate_security_config()
    logger.info(
        "Runtime config: ENV=%s DEV_MODE=%s HOST=%s PORT=%s CORS_ORIGINS=%s",
        os.getenv("ENV", "development"),
        os.getenv("DEV_MODE", "false"),
        os.getenv("HOST", "0.0.0.0"),
        os.getenv("PORT", "8000"),
        ",".join(config.get("server", {}).get("cors_origins", [])),
    )
    # Initialize database
    init_db()
    logger.info("Database initialized")

    # Initialize file storage backend (local or S3/MinIO) from config
    storage_cfg = config.get("storage", {})
    init_storage(storage_cfg)
    logger.info("Storage backend initialized: %s", storage_cfg.get("backend", "local"))

    # Initialize LLM Router
    llm_config = config.get("llm", {})
    llm_router = LLMRouter(llm_config)
    logger.info(f"LLM Router initialized with provider: {llm_config.get('provider', 'openrouter')}")

    # Initialize Langfuse tracking
    init_langfuse_tracker(config)
    langfuse_enabled = config.get("langfuse", {}).get("enabled", False)
    if langfuse_enabled:
        logger.info("Langfuse tracking initialized")
    else:
        logger.info("Langfuse tracking disabled")

    # Run LLM health checks
    logger.info("Running LLM provider health checks...")
    try:
        health_results = await llm_router.health_check()

        # Summary
        healthy_count = sum(
            1 for result in health_results.values() if result["status"] == "healthy"
        )
        total_configured = sum(
            1 for result in health_results.values() if result["status"] != "unconfigured"
        )

        if healthy_count > 0:
            logger.info(f"✅ {healthy_count}/{total_configured} LLM provider(s) ready")
        elif total_configured > 0:
            logger.warning("⚠️  No LLM providers are working - check your API keys in .env")
        else:
            logger.warning("⚠️  No LLM providers configured - add API keys to .env file")

    except Exception as e:
        logger.error(f"Health check failed: {e}")

    # Initialize Orchestrator
    orchestrator = OrchestratorAgent(llm_router, config=config)
    logger.info("Orchestrator Agent initialized")

    # Initialize A2A Protocol
    a2a_config_dict = config.get("a2a", {})
    a2a_protocol = None

    if a2a_config_dict.get("enabled", False):
        try:
            a2a_config = A2AConfig(
                enabled=True,
                agent_id=a2a_config_dict.get("agent_card", {}).get("id", "stych-agent"),
                agent_name=a2a_config_dict.get("agent_card", {}).get("name", "Stych Agent"),
                description=a2a_config_dict.get("agent_card", {}).get(
                    "description", "Data analysis agent"
                ),
                version=a2a_config_dict.get("agent_card", {}).get("version", "1.0.0"),
                capabilities=a2a_config_dict.get("agent_card", {}).get("capabilities", []),
                host=a2a_config_dict.get("server", {}).get("host", "0.0.0.0"),
                port=a2a_config_dict.get("server", {}).get("port", 8001),
            )

            a2a_protocol = StychA2AProtocol(a2a_config, orchestrator)
            logger.info("A2A Protocol initialized")

            # Start A2A server in background
            import asyncio

            asyncio.create_task(a2a_protocol.start())

        except Exception as e:
            logger.error(f"Failed to initialize A2A protocol: {e}")

    # Set dependencies
    set_dependencies(orchestrator, a2a_protocol, config)

    # Attach llm_router to app.state so per-agent routes (cleaning, linking)
    # can access it directly via req.app.state.llm_router. The orchestrator
    # holds its own reference; this just makes the same instance visible
    # at the FastAPI app level.
    app.state.llm_router = llm_router

    # Set health check start time
    health_check.start_time = time.time()

    logger.info("Stych backend started successfully")

    # Background task: flush dirty session snapshots to S3/MinIO every 5 minutes.
    # This replaces the per-query snapshot that used to hit S3 on every response.
    # Sessions are marked dirty after each query; this task batches the writes.
    import asyncio as _asyncio

    from agents.state import state_manager as _state_manager

    # Give the state manager a reference to the LLM router so it can generate
    # descriptive session titles in the background after the first assistant reply.
    _state_manager.set_llm_router(llm_router)

    async def _periodic_snapshot_flush():
        while True:
            await _asyncio.sleep(300)  # 5 minutes
            try:
                await _state_manager.snapshot_all_dirty()
            except Exception as _exc:
                logger.warning("Periodic snapshot flush error: %s", _exc)

    _snapshot_task = _asyncio.create_task(_periodic_snapshot_flush())

    yield

    # Shutdown — flush any remaining dirty sessions before exit
    logger.info("Shutting down Stych backend...")
    _snapshot_task.cancel()
    try:
        await _state_manager.snapshot_all_dirty()
        logger.info("Final snapshot flush complete")
    except Exception as exc:
        logger.warning("Final snapshot flush failed: %s", exc)

    from execution.duckdb import duckdb_kernel_manager as _duckdb_kernels

    closed_kernels = _duckdb_kernels.close_all()
    if closed_kernels:
        logger.info("Closed %d DuckDB session kernel(s)", closed_kernels)

    if a2a_protocol:
        await a2a_protocol.stop()

    logger.info("Stych backend shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Stych API",
    description="AI-powered data analysis and cleaning agent",
    version="1.0.0",
    lifespan=lifespan,
)

# Add SuperTokens middleware — must be added before CORSMiddleware so that
# Starlette's LIFO middleware stack runs CORS outermost (first on request).
from supertokens_python import get_all_cors_headers
from supertokens_python.framework.fastapi import get_middleware as get_st_middleware

app.add_middleware(get_st_middleware())

# Add CORS middleware — runs before ST middleware thanks to LIFO ordering.
# SuperTokens requires its own headers to be present in the CORS allow-list.
#
# CORS_ORIGINS env var (comma-separated) takes priority over the yaml list so
# that .env is always the single source of truth for this setting.
_cors_env = os.getenv("CORS_ORIGINS", "")
if _cors_env:
    cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    cors_origins = config.get("server", {}).get(
        "cors_origins",
        ["http://localhost:3000", "http://localhost:5173"],
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "POST", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type"] + get_all_cors_headers(),
)


# Exception handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": (str(exc) if config.get("server", {}).get("debug", False) else None),
        },
    )


# Include routers
app.include_router(auth_router)
app.include_router(session_router)

# Only expose the /api/auth/dev endpoint when DEV_MODE is explicitly enabled.
# In production the URL simply doesn't exist — not just gated at runtime.
_dev_mode_enabled = os.getenv("DEV_MODE", "false").lower() == "true"
if _dev_mode_enabled:
    app.include_router(auth_dev_router)
    logger.info("Dev auth route registered (DEV_MODE=true)")
app.include_router(router)
app.include_router(agent_run_router)
app.include_router(cleaning_export_router, prefix="/api")
app.include_router(export_router, prefix="/api")
app.include_router(preview_router, prefix="/api")
app.include_router(filter_router, prefix="/api")
app.include_router(datasource_router)
app.include_router(cloud_datasource_router)
app.include_router(connection_router)

# Register individual agent routers for distributed architecture
app.include_router(analysis_router)
app.include_router(cleaning_router)
app.include_router(qa_router)
app.include_router(orchestrator_router)
app.include_router(formatter_router)
app.include_router(viz_router)
app.include_router(ontology_router)
app.include_router(sql_agent_router)
app.include_router(csv_agent_router)
app.include_router(linking_router)
app.include_router(inspect_api_router)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Stych API",
        "version": "1.0.0",
        "description": "AI-powered data analysis and cleaning agent",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    host = config.get("server", {}).get("host", "0.0.0.0")
    port = int(config.get("server", {}).get("port", 8000))
    debug = config.get("server", {}).get("debug", False)

    # Only enable auto-reload if explicitly requested
    # Set ENABLE_RELOAD=true in .env to enable auto-reload
    enable_reload = os.getenv("ENABLE_RELOAD", "false").lower() == "true"

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=enable_reload,
        log_level="info",
    )

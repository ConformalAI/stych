"""
Data Cleaning Agent for handling data quality issues.

When data comes from a SQL source (Postgres), delegates to SQLAgent to run
cleaning on a temporary table instead of loading full data into memory.
"""

import hashlib
import json
import logging
import re
import shutil
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pandas as pd

from agents.base.agent import BaseStychAgent
from agents.cleaning.exporter import CleaningExporter
from agents.progress import _active_parent_part_id
from helpers.storage import storage_manager
from llm.infer import LLMRouter
from llm.models import Message
from react_engine.loop import (
    PauseSignal,
    deserialize_transcript,
    output_cap_from_config,
    run_agent_loop,
    transcript_budget_from_config,
)
from react_engine.schema import ReactStepRecord
from react_engine.toolkit import Toolkit
from tools.cleaning_tools import MONGO_TOOL_SCHEMAS, SQL_TOOL_SCHEMAS, CleaningReactToolRegistry
from tools.data_tools import DataAnalysisTools, DataCleaningTools
from tracing.trace_parts import emit_react_part
from workspace.manager import (
    WorkspaceManager,
    dataset_stem,
    seed_all_datasets,
)
from workspace.runner import WorkspaceSandboxRunner

logger = logging.getLogger(__name__)

# What to call each engine when instructing the model which SQL to write.
_ENGINE_LABELS = {
    "postgres": "PostgreSQL",
    "databricks": "Databricks SQL",
    "bigquery": "BigQuery Standard SQL",
    "snowflake": "Snowflake SQL",
    "redshift": "Amazon Redshift SQL",
    "mongodb": "MongoDB",
}

STATUS_COMPLETED = "completed"
STATUS_AWAITING_USER_ACTION = "awaiting_user_action"
STATUS_ERROR = "error"

# ---------------------------------------------------------------------------
# Native tool schemas for the cleaning ReAct loop.
# CSV-only tools — used when has_sql=False.
# ---------------------------------------------------------------------------
_CLEANING_TOOLS_BASE = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the cleaning workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact, unique substring in a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": (
                "Execute a Python script in the sandbox. Output is ephemeral — stdout is "
                "not saved or shown to the user. Use write_file to persist results/reports."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Script path (relative)"},
                    "code": {
                        "type": "string",
                        "description": "Inline Python code to run",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "Ask the user a clarifying question or request permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        },
                    },
                    "recommended": {"type": "string"},
                },
                "required": ["question"],
            },
        },
    },
]

# Additional SQL tools appended when has_sql=True. The schemas live next to their
# handlers in tools/cleaning_tools.py (single source of truth) so the advertised
# parameter names cannot drift from what the handlers read.
_CLEANING_TOOLS_SQL_EXTRA = SQL_TOOL_SCHEMAS
_CLEANING_TOOLS_MONGO_EXTRA = MONGO_TOOL_SCHEMAS

REACT_MAX_STEPS_DEFAULT = 50
# Single pending key for the unified-engine path (carries kind + engine record).
CLEANING_ENGINE_PENDING_KEY = "cleaning_engine_pending"

# SQL working-copy writes and persistent database writes are different scopes.
# The former are already durable Stych artifacts; the latter create/update objects
# outside Stych's scratch namespace and require explicit, operation-scoped consent.
CLEANING_WRITEBACK_REQUESTED_KEY = "cleaning_writeback_requested"
CLEANING_MATERIALIZED_KEY = "cleaning_materialized_ok"
CLEANING_PERMISSION_RECEIPTS_KEY = "cleaning_permission_receipts"
CLEANING_WRITEBACK_ATTEMPTED_KEY = "cleaning_writeback_attempted"
CLEANING_WRITEBACK_NUDGES_KEY = "cleaning_writeback_nudges"
_MAX_WRITEBACK_NUDGES = 1
_PERSISTENT_PERMISSION_TOOLS = frozenset(
    {"sql_materialize_copy", "sql_write_copy", "create_copy_table", "create_view"}
)
_WRITEBACK_INTENT_PATTERNS = (
    re.compile(r"\bwrite(?:\s+\w+){0,5}\s+back\b", re.I),
    re.compile(
        r"\bpersist(?:\s+\w+){0,6}\s+(?:to|in|into)\s+(?:the\s+)?" r"(?:database|warehouse)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:save|write|persist|materialize|create|update|replace|overwrite)\b.{0,80}"
        r"\b(?:permanent|persistent|database|warehouse|copy table)\b",
        re.I,
    ),
)
_VIEW_INTENT_PATTERN = re.compile(
    r"\b(?:create|make)(?:\s+or\s+replace)?(?:\s+a)?\s+(?:database\s+)?view\b",
    re.I,
)

if TYPE_CHECKING:
    from ..progress import ProgressEmitter
    from ..state import AgentState


class DataCleaningAgent(BaseStychAgent):
    """Agent for data cleaning and preprocessing."""

    def __init__(
        self,
        llm_router: LLMRouter,
        sql_agent: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name="DataCleaningAgent",
            description="Cleans and preprocesses datasets",
            llm_router=llm_router,
        )

        self._config = config or {}
        self.sql_agent = sql_agent
        self.csv_agent = None

        self.system_prompt = """You are a data cleaning expert. You can:
- Identify and handle missing values
- Detect and remove duplicates
- Find and handle outliers
- Generate data cleaning code
- Recommend cleaning strategies

Always explain your cleaning rationale and provide code when requested."""

        self.tools = DataCleaningTools()
        self.exporter = CleaningExporter(self)
        # WorkspaceManager resolves base_dir from config (`workspace`, falling back to
        # legacy `cleaning_workspace`) and the WORKSPACE_BASE/CLEANING_WORKSPACE_BASE envs.
        workspace_cfg = (self._config or {}).get("workspace", {}) or (self._config or {}).get(
            "cleaning_workspace", {}
        )
        self.workspace_manager = WorkspaceManager(base_dir=workspace_cfg.get("base_dir"))
        self.workspace_runner = WorkspaceSandboxRunner(timeout_seconds=60)

    async def process_query(
        self,
        state: "AgentState",
        message: str,
        user_action: Optional[Dict[str, Any]] = None,
        progress_emitter: Optional["ProgressEmitter"] = None,
    ) -> Dict[str, Any]:
        return await self._execute_react(
            state=state,
            message=message,
            user_action=user_action,
            progress_emitter=progress_emitter,
        )

    async def _execute_react(
        self,
        state: "AgentState",
        message: str,
        user_action: Optional[Dict[str, Any]],
        progress_emitter: Optional["ProgressEmitter"],
    ) -> Dict[str, Any]:
        def _standard_response(
            status: str,
            response: str,
            action_prompt: Optional[Dict[str, Any]] = None,
            metadata: Optional[Dict[str, Any]] = None,
            **extra: Any,
        ) -> Dict[str, Any]:
            payload = {
                "status": status,
                "response": response,
                "action_prompt": action_prompt,
                "metadata": metadata or {},
            }
            payload.update(extra)
            return payload

        if not state.data_context:
            return _standard_response(STATUS_COMPLETED, "Please upload a dataset first to clean.")

        from datasources.contracts import FeatureAvailability, ProductFeature  # noqa: PLC0415
        from datasources.feature_resolver import resolve_feature  # noqa: PLC0415
        from datasources.registry import get_manifest  # noqa: PLC0415

        source = (state.data_context.metadata or {}).get("source", "upload")
        try:
            clean = resolve_feature(get_manifest(source), ProductFeature.CLEAN)
        except KeyError:
            clean = None
        if clean is not None and clean.availability == FeatureAvailability.UNAVAILABLE:
            return _standard_response(STATUS_COMPLETED, clean.user_message)

        async def _emit(msg: str, **extra: Any) -> None:
            if progress_emitter:
                await progress_emitter.emit("cleaning", msg, **extra)

        # Unified-engine cleaning loop; HITL (question/permission) handled inside.
        engine_pending = state.metadata.get(CLEANING_ENGINE_PENDING_KEY)
        if engine_pending:
            return await self._resume_engine(
                state=state,
                pending=engine_pending,
                user_action=user_action,
                progress_emitter=progress_emitter,
            )
        return await self._run_unified(
            state=state, message=message, progress_emitter=progress_emitter
        )

    def compute_quality_metrics(self, dataframe: pd.DataFrame) -> Dict[str, Any]:
        duplicates = int(dataframe.duplicated().sum())
        missing_report = DataAnalysisTools.missing_values_report(dataframe)
        numeric_cols = dataframe.select_dtypes(include=["number"]).columns
        outlier_reports: Dict[str, Any] = {}
        for col in numeric_cols:
            report = self.tools.detect_outliers(dataframe, col)
            if not report.get("error"):
                outlier_reports[col] = report
        return {
            "duplicates": {
                "count": duplicates,
                "percentage": round(duplicates / max(len(dataframe), 1) * 100, 2),
            },
            "missing_values": missing_report,
            "outliers": outlier_reports,
        }

    async def analyze_quality(self, dataframe: pd.DataFrame) -> Dict[str, Any]:
        try:
            issues = self.compute_quality_metrics(dataframe)
            messages = [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Analyze these data quality issues and recommend cleaning steps:\n\n"
                        f"Issues Found:\n{issues}\n\n"
                        "Provide specific, actionable recommendations."
                    ),
                },
            ]
            recommendations = await self.generate_response(messages)
            return {"issues": issues, "recommendations": recommendations}
        except Exception as e:
            logger.error(f"Error analyzing data quality: {e}")
            return {"error": str(e)}

    async def generate_export_readme(
        self,
        *,
        code: str,
        plan: Dict[str, Any],
        choices: Dict[str, str],
        dataset_summary: Dict[str, Any],
    ) -> str:
        return await self.exporter.generate_export_readme(
            code=code,
            plan=plan,
            choices=choices,
            dataset_summary=dataset_summary,
        )

    async def generate_export_readme_sql(
        self,
        *,
        sql: str,
        plan: Dict[str, Any],
        choices: Dict[str, str],
        dataset_summary: Dict[str, Any],
    ) -> str:
        return await self.exporter.generate_export_readme_sql(
            sql=sql,
            plan=plan,
            choices=choices,
            dataset_summary=dataset_summary,
        )

    def _cleaning_config(self) -> Dict[str, Any]:
        return self._config.get("cleaning", {})

    # ------------------------------------------------------------------
    # ReAct loop + helpers
    # ------------------------------------------------------------------

    def _react_prompt(
        self,
        has_sql: bool = False,
        sql_dialect: str = "postgres",
        is_mongo: bool = False,
    ) -> str:
        """
        Build the ReAct system prompt.

        ``has_sql=True`` adds SQL tool definitions.  When False, SQL tools are
        completely omitted from the tool list so the model never attempts to call them.
        """
        engine_label = _ENGINE_LABELS.get(sql_dialect, "PostgreSQL")
        if is_mongo:
            tool_list = (
                "- mongo_read         [MongoDB aggregation on the active working copy]\n"
                "- mongo_update       [MongoDB update or update pipeline on the working copy]\n"
                "- mongo_delete       [delete documents from the working copy]\n"
                "- mongo_deduplicate  [deduplicate the working copy by selected fields]\n"
                "- create_working_copy / switch_working_copy / drop_working_copy / "
                "list_working_copies / diff_working_copies  [manage private MongoDB working "
                "copies]\n"
                "- question (ask user; pauses execution)\n"
            )
            sql_rules = (
                "6) MongoDB datasource — clean with the mongo_* tools, never SQL or pandas.\n"
                "   - The tools lazily create a private working copy in the Stych scratch "
                "database. The user's source collection is never modified.\n"
                "   - mongo_read accepts a JSON aggregation pipeline. Cross-collection stages "
                "($lookup, $unionWith, $graphLookup), write stages, and server-side JavaScript "
                "are refused.\n"
                "   - mongo_update accepts a JSON filter and either an update document or an "
                "aggregation update pipeline. Use it for fills, casts, standardization, field "
                "renames, and other document transforms.\n"
                "   - mongo_delete removes matching documents from the private working copy.\n"
                "   - mongo_deduplicate keeps the lowest _id for each selected field tuple.\n"
                "7) Changes returned successfully by mongo_update, mongo_delete, or "
                "mongo_deduplicate are already persisted in the active private working copy and "
                "are available to preview and diff. Never claim the source collection changed.\n"
            )
            extra_rules = (
                "8) Only call the MongoDB and working-copy tools listed above; workspace file "
                "tools and SQL tools are unavailable for this datasource.\n"
                "9) Never include markdown fences in your response.\n"
            )
        elif has_sql:
            tool_list = (
                "- list_files\n"
                "- read_file\n"
                "- write_file\n"
                "- edit_file\n"
                "- delete_file\n"
                "- run_script\n"
                f"- sql_read         [{engine_label} — active, database connection is present]\n"
                f"- sql_preview      [{engine_label} — active]\n"
                f"- sql_materialize_copy  [{engine_label} — PERMANENT database copy; only when "
                "explicitly requested; requires user approval]\n"
                f"- sql_write_temp   [{engine_label} — active]\n"
                f"- sql_write_copy   [{engine_label} — active, requires user approval]\n"
                f"- create_copy_table    [{engine_label} — OPTIONAL/legacy; sql_materialize_copy "
                "already creates the copy table, so you normally do NOT need this]\n"
                f"- create_view      [{engine_label} — active, requires user approval]\n"
                "- create_working_copy / switch_working_copy / drop_working_copy / "
                f"list_working_copies / diff_working_copies  [{engine_label} — manage the "
                "session's named working copies; create a fresh branch from the original when "
                "the user wants to start over or try a different approach]\n"
                "- question (ask user; pauses execution)\n"
            )
            dialect_note = (
                "   - Write Databricks SQL. Quote 3-level names with backticks "
                "(`catalog`.`schema`.`table`). {table} is the scratch clone — never UPDATE the "
                "user's source table. Use CAST(x AS DOUBLE)/CAST(x AS STRING); do not use "
                "Postgres-only syntax (to_char, ::numeric, ~).\n"
                if sql_dialect == "databricks"
                else ""
            )
            sql_rules = (
                "6) SQL datasource — clean on the database, NOT in pandas. Do NOT use run_script "
                "for cleaning a SQL datasource; in-memory pandas edits never reach the database "
                "copy table.\n"
                "   - ALWAYS reference the working table using the {table} placeholder; never "
                "guess table names like 'data' or 'temp_cafe_data'.\n"
                "   - {table} is the session's ACTIVE working copy (scratch schema) of the "
                "user's source table. Modifying it is safe, its contents are what the user sees "
                "in the app's data preview/diff, and the original table is never touched. "
                "create_working_copy makes a fresh branch when the user wants to start over.\n"
                f"{dialect_note}"
                '   - sql_read/sql_preview take {"sql": "..."} and RETURN your query\'s result '
                "(a CTE with WITH is allowed); they do not dump the whole table.\n"
                '   - sql_write_temp takes {"sql": "..."} (DML/DDL, not SELECT) against {table}. '
                "It accepts multiple semicolon-separated statements in one call.\n"
                "7) Persistence facts (SQL sessions):\n"
                "   - sql_write_temp changes are immediately durable in the active private Stych "
                "working copy and are what preview/diff/analysis read. If the user asks for a "
                "working copy only, STOP after verification; do not call any persistent-write "
                "tool and do not ask for database write-back permission.\n"
                "   - sql_materialize_copy creates or replaces a separate persistent table in "
                "the user's database schema. Call it ONLY when the user explicitly asks for a "
                "permanent/database copy table. It prompts once for approval. Never treat the "
                "words 'working copy' as permission to materialize a database table.\n"
                "   - sql_write_copy is ONLY for in-place edits on an existing copy table "
                "(UPDATE/DELETE/ALTER); it does not accept SELECT and is not the write-back path.\n"
                "   - sql_materialize_copy/sql_write_copy/create_copy_table/create_view require "
                "explicit user approval (one approval each).\n"
            )
            extra_rules = (
                "8) The active dataset is a parquet file (typed); its "
                "exact name is given in the context below — use it, do not assume 'data.parquet'. "
                "To persist a cleaned dataset, write it back to that same parquet file.\n"
                "9) Sandbox restrictions — blocked imports: os, sys, subprocess, socket, shutil, "
                "pathlib, requests, httpx, urllib (network is disabled; file access is confined to "
                "the workspace). All standard Python builtins ARE available "
                "(next, iter, map, filter, any, all, isinstance, type, hasattr, getattr, vars, etc.).\n"
                "10) Any installed library may be imported (pandas, numpy, scipy, scikit-learn, "
                "statsmodels, …) plus the standard library.\n"
                "11) You may run scripts in the sandbox workspace to explore data.\n"
                '    - run_script input must be {"code": "...python..."} or {"path": "script.py"}.\n'
                "    - Load the dataset with pd.read_parquet on the active dataset file named "
                "in the context.\n"
                "    - Each run_script is a fresh process — re-load the data every time.\n"
                "12) Never include markdown fences in your response.\n"
            )
        else:
            tool_list = (
                "- list_files\n"
                "- read_file\n"
                "- write_file\n"
                "- edit_file\n"
                "- delete_file\n"
                "- run_script\n"
                "- question (ask user; pauses execution)\n"
            )
            sql_rules = (
                "6) SQL tools (sql_read, sql_preview, sql_write_*, create_*) are NOT available "
                "in this session — no database connection is active. Do NOT call them.\n"
            )
            extra_rules = (
                "7) The active dataset is a parquet file (typed); its "
                "exact name is given in the context below — use it, do not assume 'data.parquet'. "
                "To persist a cleaned dataset, write it back to that same parquet file.\n"
                "8) Sandbox restrictions — blocked imports: os, sys, subprocess, socket, shutil, "
                "pathlib, requests, httpx, urllib (network is disabled; file access is confined to "
                "the workspace). All standard Python builtins ARE available "
                "(next, iter, map, filter, any, all, isinstance, type, hasattr, getattr, vars, etc.).\n"
                "9) Any installed library may be imported (pandas, numpy, scipy, scikit-learn, "
                "statsmodels, …) plus the standard library.\n"
                "10) You may run scripts in the sandbox workspace to explore data.\n"
                '    - run_script input must be {"code": "...python..."} or {"path": "script.py"}.\n'
                "    - Load the dataset with pd.read_parquet on the active dataset file named "
                "in the context.\n"
                "    - For large files (>5 MB or >50k rows), use chunked reading:\n"
                "      for chunk in pd.read_csv('file.csv', chunksize=5000, on_bad_lines='skip'): ...\n"
                "11) Never include markdown fences in your response.\n"
            )

        return (
            "You are a data cleaning agent using a ReAct loop. "
            "You can think in freeform text, then call tools when needed.\n\n"
            "Plan your own approach — you decide what to inspect, how to batch the work, and "
            "when you are done. Each LLM step is expensive: prefer fewer, richer steps (one "
            "script or one multi-statement SQL call can inspect, clean, and verify together) "
            "over many small ones.\n\n"
            f"Available tools:\n{tool_list}\n"
            "Rules:\n"
            "1) Call one of the tools listed above (or question) to act.\n"
            "   - When you are done, output your summary as plain text without calling any tool.\n"
            "2) Use question when you need user permission or clarification.\n"
            "   - When calling question, input MUST be an object with:\n"
            '     {"question": "...", "options": [{"label":"...","value":"..."}], '
            '"recommended": "..." (optional)}\n'
            "     The UI always includes a free-text reply field; do not try to disable it.\n"
            "3) Do NOT include options in your final summary. Use question instead.\n"
            "4) Use ONLY workspace-relative paths for read_file/write_file/run_script. "
            "Do not use absolute paths or upload paths.\n"
            "5) Only call tools listed above. Any unlisted tool will return an error.\n"
            f"{sql_rules}"
            f"{extra_rules}"
        )

    def _prepare_workspace(self, state: "AgentState") -> str:
        """
        Ensure the session workspace exists and, for file datasets, place a copy
        of the uploaded file in the workspace for the agent to read.

        Supports both local and S3 storage backends:
        - Local: fast path via filesystem copy (get_upload_path)
        - S3/remote: download bytes then write to workspace (get_upload_bytes)
        """
        user_id = state.metadata.get("user_id")
        workspace = self.workspace_manager.get_or_create(state.session_id, user_id=user_id)
        # Restore archived workspace files from storage (e.g. after server restart or new pod)
        # when the workspace directory is empty.
        if not any(workspace.iterdir()):
            self.workspace_manager.restore_from_storage(state.session_id, user_id, storage_manager)
        if not state.data_context or state.data_context.is_sql_datasource():
            return f"Workspace: {workspace}"
        file_id = state.data_context.file_id
        filename = state.data_context.filename
        dest = workspace / filename
        should_copy = True
        if dest.exists():
            last_file_id = state.metadata.get("cleaning_workspace_file_id")
            try:
                size = dest.stat().st_size
            except Exception:
                size = 0
            should_copy = (last_file_id != file_id) or size == 0
        if should_copy:
            # Fast path: local backend exposes a filesystem path — use direct copy.
            src = storage_manager.get_upload_path(file_id, filename)
            if src:
                try:
                    shutil.copy2(src, dest)
                except Exception as exc:
                    logger.warning(
                        "[CleaningReAct] workspace_copy_failed src=%s dest=%s error=%s",
                        src,
                        dest,
                        exc,
                    )
            else:
                # Fallback path: S3 or any remote backend — download bytes and write.
                content = storage_manager.get_upload_bytes(file_id, filename)
                if content:
                    try:
                        dest.write_bytes(content)
                    except Exception as exc:
                        logger.warning(
                            "[CleaningReAct] workspace_write_failed dest=%s error=%s",
                            dest,
                            exc,
                        )
                else:
                    logger.warning(
                        "[CleaningReAct] workspace_copy_missing file_id=%s filename=%s",
                        file_id,
                        filename,
                    )
                    return f"Workspace: {workspace} (uploaded file not found)"
        state.metadata["cleaning_workspace_file"] = str(dest)
        state.metadata["cleaning_workspace_file_id"] = file_id

        # Seed every dataset (primary + additional) so cleaning/analysis share one
        # workspace and can read any dataset directly; active_dataset only marks the
        # default working frame. Missing files are (re)seeded without clobbering existing
        # ones (which may hold prior cleaning).
        try:
            if not state.metadata.get("active_dataset"):
                state.metadata["active_dataset"] = dataset_stem(state.data_context.filename)
            seed_all_datasets(workspace, state)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("[CleaningReAct] workspace_seed_failed: %s", exc)

        active_stem = state.metadata.get("active_dataset") or dataset_stem(
            state.data_context.filename
        )
        others = [
            dataset_stem(dc.filename)
            for dc in getattr(state, "additional_data_contexts", None) or []
            if dc is not None and not dc.is_sql_datasource()
        ]
        others = [s for s in others if s and s != active_stem]
        others_line = (
            f" Other datasets are also in the workspace and can be read directly: "
            f"{', '.join(f'`{s}.parquet`' for s in others)}."
            if others
            else ""
        )
        return (
            f"Workspace: {workspace}\n"
            f"The active dataset is `{active_stem}.parquet` (typed). "
            f"Read it with pd.read_parquet('{active_stem}.parquet'); to persist a cleaned "
            f"dataset write it back to that same file. Call list_files to see all files.{others_line} "
            f"Name any new dataset file you create with a suffix indicating the change "
            f"(e.g. `{active_stem}_imputed.parquet`) — never a generic "
            f"name like output.csv."
        )

    # ------------------------------------------------------------------
    # Unified-engine path (agents.use_unified_engine = true)
    # ------------------------------------------------------------------

    def _unified_setup(
        self,
        state: "AgentState",
        message: str,
        progress_emitter: Optional["ProgressEmitter"],
    ) -> Dict[str, Any]:
        """Shared engine wiring for a fresh cleaning run and a HITL resume."""
        cfg = self._cleaning_config()
        max_steps = int(cfg.get("react_max_steps", REACT_MAX_STEPS_DEFAULT))
        has_source = bool(state.data_context and state.data_context.is_sql_datasource())
        source = (state.data_context.metadata or {}).get("source") if state.data_context else None
        is_mongo = source == "mongodb"
        has_sql = has_source and not is_mongo
        # The engine the agent actually authors for. This used to coerce every
        # non-Databricks source to "postgres", which told the model to write
        # Postgres SQL against BigQuery, Snowflake and Redshift.
        sql_dialect = "postgres"
        if has_sql:
            sql_dialect = source or "postgres"
        df = state.data_context.working_dataframe
        workspace_hint = self._prepare_workspace(state)
        tools = CleaningReactToolRegistry(
            sql_agent=self.sql_agent,
            workspace_manager=self.workspace_manager,
            workspace_runner=self.workspace_runner,
            is_sql_available=has_sql,
            is_mongo_available=is_mongo,
        )
        pause_ctx: Dict[str, Any] = {}

        if is_mongo:
            env_state = (
                "Environment State:\n"
                "- Data source: MongoDB\n"
                "- MongoDB cleaning tools: ACTIVE\n"
                "- SQL and workspace file tools: unavailable\n\n"
                "Rules:\n"
                "- Inspect and clean only the attached collection's private working copy.\n"
                "- Never modify the attached source collection or read another collection.\n"
            )
        elif has_sql:
            engine_name = _ENGINE_LABELS.get(sql_dialect, "PostgreSQL")
            env_state = (
                "Environment State:\n"
                f"- Data source: {engine_name}\n"
                "- SQL tools: ACTIVE (database connection is present)\n"
                "- CSV workspace: available for intermediate files\n\n"
                "Rules:\n"
                "- Use the sql_* tools to query and modify the scratch working copy.\n"
                "- Never UPDATE the user's source table.\n"
                "- run_script runs in a sandbox with NO database access (no psycopg2, no os/"
                "network); never use it to read or query the table — use sql_read/sql_preview. "
                "Use run_script only for Python-side transforms on workspace files.\n"
            )
        else:
            env_state = (
                "Environment State:\n"
                "- Data source: CSV file\n"
                "- File in workspace: YES\n"
                "- PostgreSQL connected: NO\n\n"
                "Rules:\n"
                "- Only use file/workspace tools (list_files, read_file, write_file, run_script).\n"
                "- Do NOT call any sql_* or create_* tools — they are unavailable for this session.\n"
            )
        schema_summary = "\n".join(
            f"  {col} ({dtype})" for col, dtype in df.dtypes.astype(str).items()
        )
        system_content = (
            f"{self._react_prompt(has_sql=has_sql, sql_dialect=sql_dialect, is_mongo=is_mongo)}\n\n"
            f"{env_state}\n"
            f"Dataset shape: {df.shape[0]} rows × {df.shape[1]} columns\n"
            f"Columns:\n{schema_summary}\n"
            f"{workspace_hint}"
        )
        if is_mongo:
            question_tool = [
                schema
                for schema in _CLEANING_TOOLS_BASE
                if schema["function"]["name"] == "question"
            ]
            cleaning_tools = question_tool + _CLEANING_TOOLS_MONGO_EXTRA
        elif has_sql:
            cleaning_tools = _CLEANING_TOOLS_BASE + _CLEANING_TOOLS_SQL_EXTRA
        else:
            cleaning_tools = _CLEANING_TOOLS_BASE

        async def _tool_executor(name: str, args: Any, _call_id: str) -> str:
            if name == "question":
                normalized = self._normalize_question_input(args)
                prompt_payload = self._build_react_question(normalized)
                pause_ctx.update(
                    {
                        "kind": "cleaning_react_question",
                        "action_prompt": prompt_payload,
                        "question": prompt_payload["question"],
                    }
                )
                raise PauseSignal(prompt_payload, response=prompt_payload["question"])
            if name in _PERSISTENT_PERMISSION_TOOLS:
                permission_key = self._permission_key(name, args)
                receipt = (state.metadata.get(CLEANING_PERMISSION_RECEIPTS_KEY) or {}).get(
                    permission_key
                )
                if receipt:
                    return (
                        "PERMISSION_ALREADY_RESOLVED: this exact operation was already handled; "
                        f"do not request permission again. Result: {receipt.get('observation', '')}"
                    )
                if not self._persistent_permission_requested(name, message, state):
                    return (
                        "SCOPE_ERROR: the user requested only a private Stych working copy. "
                        f"Do not call {name}, create a permanent database object, or ask for "
                        "write-back permission. Finish after verifying the scratch working copy."
                    )
                prompt_payload = self._build_permission_prompt(name, args)
                pause_ctx.update(
                    {
                        "kind": "cleaning_react_permission",
                        "action_prompt": prompt_payload,
                        "question": prompt_payload["question"],
                        "tool": name,
                        "tool_input": args,
                    }
                )
                raise PauseSignal(prompt_payload, response=prompt_payload["question"])
            observation = await tools.call(tool_name=name, tool_input=args, state=state)
            return observation

        async def _emitter(rec: ReactStepRecord) -> None:
            await emit_react_part(
                progress_emitter, "cleaning", rec, parent_id=_active_parent_part_id.get()
            )

        async def _finalizer(text: str, _transcript: List[Message]) -> Dict[str, Any]:
            return {
                "status": STATUS_COMPLETED,
                "response": text,
                "metadata": {"execution_mode": "react"},
            }

        async def _budget_summarizer(transcript: List[Message]) -> str:
            trace = [
                {"step": i + 1, "action": m.role, "observation": m.content or ""}
                for i, m in enumerate(transcript)
                if m.role == "tool"
            ]
            return self._summarize_trace(trace, message)

        toolkit = Toolkit.from_schemas(
            cleaning_tools,
            lambda name, args: _tool_executor(name, args, ""),
            known_names=CleaningReactToolRegistry.TOOL_NAMES | {"question"},
            hitl={
                "question",
                "sql_materialize_copy",
                "sql_write_copy",
                "create_copy_table",
                "create_view",
            },
            parallel_safe={"list_files", "read_file"},
        )

        return {
            "toolkit": toolkit,
            "emitter": _emitter,
            "finalizer": _finalizer,
            "budget_summarizer": _budget_summarizer,
            "system_content": system_content,
            "pause_ctx": pause_ctx,
            "max_steps": max_steps,
        }

    async def _run_unified(
        self,
        state: "AgentState",
        message: str,
        progress_emitter: Optional["ProgressEmitter"],
    ) -> Dict[str, Any]:
        # Fresh turn: reset the write-back guard and record whether persistence was asked for.
        source = (state.data_context.metadata or {}).get("source") if state.data_context else None
        has_sql = bool(
            state.data_context and state.data_context.is_sql_datasource() and source != "mongodb"
        )
        state.metadata[CLEANING_WRITEBACK_REQUESTED_KEY] = has_sql and self._writeback_requested(
            message, state
        )
        state.metadata[CLEANING_MATERIALIZED_KEY] = False
        state.metadata[CLEANING_PERMISSION_RECEIPTS_KEY] = {}
        state.metadata[CLEANING_WRITEBACK_ATTEMPTED_KEY] = False
        state.metadata[CLEANING_WRITEBACK_NUDGES_KEY] = 0

        setup = self._unified_setup(state, message, progress_emitter)
        transcript = [
            Message(role="system", content=setup["system_content"]),
            Message(role="user", content=message),
        ]
        result = await run_agent_loop(
            llm_router=self.llm_router,
            agent_name="cleaning",
            toolkit=setup["toolkit"],
            transcript=transcript,
            max_steps=setup["max_steps"],
            temperature=0.5,
            output_token_cap=output_cap_from_config(self._config),
            transcript_token_budget=transcript_budget_from_config(self._config),
            emitter=setup["emitter"],
            finalizer=setup["finalizer"],
            budget_summarizer=setup["budget_summarizer"],
        )
        result = await self._enforce_writeback(result, state, message, setup, progress_emitter)
        return self._finish_unified(result, state, setup["pause_ctx"], message)

    @staticmethod
    def _intent_text(message: str, state: Optional["AgentState"] = None) -> str:
        """Current delegated task plus the latest real user request, never old turns."""
        texts = [message or ""]
        if state is not None:
            latest_user = next(
                (
                    m.content or ""
                    for m in reversed(getattr(state, "messages", []))
                    if getattr(m, "role", "") == "user"
                ),
                "",
            )
            texts.append(latest_user)
        return " ".join(texts)

    @classmethod
    def _writeback_requested(cls, message: str, state: Optional["AgentState"] = None) -> bool:
        """Did this turn explicitly request a permanent database table write?"""
        blob = cls._intent_text(message, state)
        return any(pattern.search(blob) for pattern in _WRITEBACK_INTENT_PATTERNS)

    @classmethod
    def _persistent_permission_requested(
        cls,
        tool: str,
        message: str,
        state: Optional["AgentState"] = None,
    ) -> bool:
        """Authorize prompting only for the persistent operation the user named."""
        blob = cls._intent_text(message, state)
        if tool == "create_view":
            return bool(_VIEW_INTENT_PATTERN.search(blob))
        return any(pattern.search(blob) for pattern in _WRITEBACK_INTENT_PATTERNS)

    @staticmethod
    def _permission_key(tool: str, tool_input: Any) -> str:
        """Stable identity for one consent-scoped database operation."""
        payload = dict(tool_input) if isinstance(tool_input, dict) else {"input": tool_input}
        payload.pop("approved", None)
        if tool == "sql_materialize_copy":
            payload.setdefault("source_target", "temp")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode()).hexdigest()[:20]
        return f"{tool}:{digest}"

    async def _enforce_writeback(
        self,
        result: Dict[str, Any],
        state: "AgentState",
        message: str,
        setup: Dict[str, Any],
        progress_emitter: Optional["ProgressEmitter"],
    ) -> Dict[str, Any]:
        """Refuse a false-success finish: if persistence was requested but
        sql_materialize_copy never succeeded, inject a corrective turn forcing the
        write-back. Bounded by _MAX_WRITEBACK_NUDGES; a pause (approval) is passed through.
        """
        # Scratch-table mutation is already the requested durable result for a private
        # working-copy task. Only explicit permanent-database intent activates this guard.
        wants_persist = bool(state.metadata.get(CLEANING_WRITEBACK_REQUESTED_KEY))
        if not wants_persist or state.metadata.get(CLEANING_WRITEBACK_ATTEMPTED_KEY):
            return result
        while (
            result.get("status") in (STATUS_COMPLETED, "partial")
            and not state.metadata.get(CLEANING_MATERIALIZED_KEY)
            and not state.metadata.get(CLEANING_WRITEBACK_ATTEMPTED_KEY)
            and state.metadata.get(CLEANING_WRITEBACK_NUDGES_KEY, 0) < _MAX_WRITEBACK_NUDGES
        ):
            state.metadata[CLEANING_WRITEBACK_NUDGES_KEY] = (
                state.metadata.get(CLEANING_WRITEBACK_NUDGES_KEY, 0) + 1
            )
            if progress_emitter:
                await progress_emitter.emit(
                    "cleaning", "Cleaned data not yet persisted — completing the write-back…"
                )
            transcript = deserialize_transcript(
                (result.get("metadata") or {}).get("transcript", [])
            )
            transcript.append(
                Message(
                    role="user",
                    content=(
                        "The user explicitly requested a permanent database copy, but none was "
                        "created. Call sql_materialize_copy once so Stych can request scoped user "
                        "approval. Do not claim a permanent table exists unless the tool returns "
                        "status:ok."
                    ),
                )
            )
            result = await run_agent_loop(
                llm_router=self.llm_router,
                agent_name="cleaning",
                toolkit=setup["toolkit"],
                transcript=transcript,
                max_steps=setup["max_steps"],
                temperature=0.5,
                emitter=setup["emitter"],
                finalizer=setup["finalizer"],
                budget_summarizer=setup["budget_summarizer"],
            )
            if result.get("status") == STATUS_AWAITING_USER_ACTION:
                break  # materialize is now pending approval — surface the pause
        return result

    def _finish_unified(
        self,
        result: Dict[str, Any],
        state: "AgentState",
        pause_ctx: Dict[str, Any],
        message: str,
    ) -> Dict[str, Any]:
        if result.get("status") == "awaiting_user_action":
            state.metadata[CLEANING_ENGINE_PENDING_KEY] = {
                "kind": pause_ctx.get("kind"),
                "action_prompt": pause_ctx.get("action_prompt"),
                "prompt_id": (pause_ctx.get("action_prompt") or {}).get("prompt_id"),
                "tool": pause_ctx.get("tool"),
                "tool_input": pause_ctx.get("tool_input"),
                "message": message,
                # run_agent_loop(resume=...) expects both keys.
                "engine_pending": {
                    "transcript": result["metadata"]["transcript"],
                    "pending": result["metadata"]["pending"],
                },
            }
            return {
                "status": STATUS_AWAITING_USER_ACTION,
                "response": result.get("response", pause_ctx.get("question", "")),
                "action_prompt": pause_ctx.get("action_prompt"),
                "metadata": {"execution_mode": "react"},
            }
        return {
            "status": STATUS_COMPLETED,
            "response": result.get("response", ""),
            "metadata": result.get("metadata", {"execution_mode": "react"}),
        }

    def _format_question_answer(
        self, action_prompt: Dict[str, Any], user_action: Dict[str, Any]
    ) -> str:
        answer_value = user_action.get("value") or user_action.get("text") or ""
        answer_label = user_action.get("label")
        if not answer_label:
            for opt in action_prompt.get("options", []) or []:
                if opt.get("value") == answer_value:
                    answer_label = opt.get("label")
                    break
        if not answer_label:
            answer_label = str(answer_value)
        free_text = user_action.get("text") or ""
        question_text = action_prompt.get("question") or "Question"
        lines = [
            f"Question: {question_text}",
            f"Answer: {answer_label} (value: {answer_value})",
        ]
        if free_text:
            lines.append(f"Free text: {free_text}")
        return "\n".join(lines)

    async def _resume_engine(
        self,
        state: "AgentState",
        pending: Dict[str, Any],
        user_action: Optional[Dict[str, Any]],
        progress_emitter: Optional["ProgressEmitter"],
    ) -> Dict[str, Any]:
        """Resume a paused unified cleaning loop (question or permission HITL)."""
        kind = pending.get("kind")
        action_prompt = pending.get("action_prompt") or {}
        expected_pid = pending.get("prompt_id")
        message = pending.get("message", "")

        def _reprompt(text: str) -> Dict[str, Any]:
            return {
                "status": STATUS_AWAITING_USER_ACTION,
                "response": text,
                "action_prompt": action_prompt,
                "metadata": {"execution_mode": "react"},
            }

        # Validate the user's answer matches the pending prompt.
        if not user_action or user_action.get("kind") != kind:
            return _reprompt(
                "Please answer the pending question to continue."
                if kind == "cleaning_react_question"
                else "Please confirm permission to proceed."
            )
        if expected_pid and user_action.get("prompt_id") != expected_pid:
            return _reprompt("Please answer the latest pending request to continue.")

        # Compute the paused tool's native result from the user's decision.
        if kind == "cleaning_react_question":
            observation = self._format_question_answer(action_prompt, user_action)
        else:  # cleaning_react_permission
            decision = str(user_action.get("value", "")).lower()
            tool = pending.get("tool")
            tool_input = dict(pending.get("tool_input") or {})
            permission_key = self._permission_key(tool, tool_input)
            if tool == "sql_materialize_copy":
                state.metadata[CLEANING_WRITEBACK_ATTEMPTED_KEY] = True
            if decision in {"accept", "yes", "y"}:
                tool_input["approved"] = True
                if progress_emitter:
                    await progress_emitter.emit("cleaning", f"Permission granted — calling {tool}…")
                source = (
                    (state.data_context.metadata or {}).get("source")
                    if state.data_context
                    else None
                )
                has_source = bool(state.data_context and state.data_context.is_sql_datasource())
                tools = CleaningReactToolRegistry(
                    sql_agent=self.sql_agent,
                    workspace_manager=self.workspace_manager,
                    workspace_runner=self.workspace_runner,
                    is_sql_available=has_source and source != "mongodb",
                    is_mongo_available=source == "mongodb",
                )
                observation = await tools.call(tool_name=tool, tool_input=tool_input, state=state)
                # Record a successful write-back so the completion guard is satisfied.
                if tool == "sql_materialize_copy" and '"status": "ok"' in observation:
                    state.metadata[CLEANING_MATERIALIZED_KEY] = True
            else:
                observation = "User denied permission."
            receipts = state.metadata.setdefault(CLEANING_PERMISSION_RECEIPTS_KEY, {})
            receipts[permission_key] = {
                "decision": decision,
                "observation": observation,
            }

        state.metadata.pop(CLEANING_ENGINE_PENDING_KEY, None)
        setup = self._unified_setup(state, message, progress_emitter)
        result = await run_agent_loop(
            llm_router=self.llm_router,
            agent_name="cleaning",
            toolkit=setup["toolkit"],
            transcript=[],
            max_steps=setup["max_steps"],
            temperature=0.5,
            output_token_cap=output_cap_from_config(self._config),
            transcript_token_budget=transcript_budget_from_config(self._config),
            emitter=setup["emitter"],
            finalizer=setup["finalizer"],
            budget_summarizer=setup["budget_summarizer"],
            resume=pending["engine_pending"],
            resume_observation=observation,
        )
        result = await self._enforce_writeback(result, state, message, setup, progress_emitter)
        return self._finish_unified(result, state, setup["pause_ctx"], message)

    def _build_react_question(self, tool_input: Any) -> Dict[str, Any]:
        prompt_id = str(uuid.uuid4())
        if isinstance(tool_input, dict):
            question = str(tool_input.get("question") or "Need your input.")
            options = tool_input.get("options") or []
            recommended = tool_input.get("recommended")
        else:
            question = str(tool_input or "Need your input.")
            options = []
            recommended = None
        if recommended:
            for opt in options:
                if opt.get("value") == recommended:
                    opt["label"] = f"{opt.get('label', opt.get('value', ''))} (Recommended)"
                    options = [opt] + [o for o in options if o is not opt]
                    break
        return {
            "prompt_id": prompt_id,
            "kind": "cleaning_react_question",
            "question": question,
            "options": options,
            # Always on: model-supplied allow_free_text must not hide the HITL text field.
            "allow_open_text": True,
            "context": {},
        }

    def _normalize_question_input(self, tool_input: Any) -> Dict[str, Any]:
        if isinstance(tool_input, dict):
            question = str(tool_input.get("question") or "Need your input.")
            options = tool_input.get("options")
            if not isinstance(options, list):
                options = []
            normalized_options = []
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or opt.get("value") or "").strip()
                value = opt.get("value")
                if value is None:
                    value = label
                normalized_options.append({"label": label, "value": value})
            return {
                "question": question,
                "options": normalized_options,
                "recommended": tool_input.get("recommended"),
            }
        return {
            "question": str(tool_input or "Need your input."),
            "options": [],
            "recommended": None,
        }

    def _build_permission_prompt(self, tool: str, tool_input: Any) -> Dict[str, Any]:
        prompt_id = str(uuid.uuid4())
        operation = {
            "sql_materialize_copy": "create or replace a permanent database copy table",
            "sql_write_copy": "modify an existing permanent database copy table",
            "create_copy_table": "create a permanent database copy table",
            "create_view": "create or replace a database view",
        }.get(tool, f"run {tool}")
        return {
            "prompt_id": prompt_id,
            "kind": "cleaning_react_permission",
            "question": f"Allow Stych to {operation}? This writes outside its scratch schema.",
            "options": [
                {"label": "Yes, proceed", "value": "accept"},
                {"label": "No, cancel", "value": "decline"},
            ],
            "allow_open_text": False,
            "context": {"tool": tool, "tool_input": tool_input},
        }

    @staticmethod
    def _summarize_trace(trace: List[Dict[str, Any]], message: str) -> str:
        if not trace:
            return "No actions were taken."
        last_obs = trace[-1].get("observation", "")
        return f"I reached the max step limit before finishing. Last observation:\n{last_obs}"

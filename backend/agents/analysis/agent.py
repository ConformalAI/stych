"""
Data Analysis Agent with cleaning readiness check and sandbox execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from agents.base.agent import BaseStychAgent
from agents.cleaning.agent import DataCleaningAgent
from agents.handoff import append_handoff_event, handoff_to_response_metadata
from agents.progress import ProgressEmitter, _active_parent_part_id
from agents.state import AgentState
from helpers.sse import SseEventType
from llm.infer import LLMRouter
from llm.models import LLMRequest, Message
from llm.usage_limit import check_usage_limit
from react_engine.loop import (
    PauseSignal,
    output_cap_from_config,
    run_agent_loop,
    transcript_budget_from_config,
)
from react_engine.schema import ReactStepRecord, preview
from react_engine.toolkit import Toolkit
from tools.analysis_tools import ReactToolRegistry
from tools.data_tools import DataAnalysisTools
from tools.sql_copies import WORKING_COPY_TOOLS
from tools.sql_write import SQL_WRITE_TEMP_TOOL
from tools.workspace_toolset import WorkspaceToolset
from tracing.trace_parts import emit_react_part
from workspace.manager import (
    WorkspaceManager,
    dataset_stem,
    seed_all_datasets,
)
from workspace.runner import WorkspaceSandboxRunner

logger = logging.getLogger(__name__)

READINESS_READY = "READY"
READINESS_NEEDS_CLEANING = "NEEDS_CLEANING"

STATUS_COMPLETED = "completed"
STATUS_AWAITING_USER_ACTION = "awaiting_user_action"
STATUS_ERROR = "error"

PENDING_KEY = "analysis_pending"
ACTION_KIND_CLEANING_CONSENT = "cleaning_consent"
PENDING_TYPE_TOOL_NEEDS_CLEANING = "tool_needs_cleaning"
ANALYSIS_REACT_INTERRUPTED_KEY = "analysis_react_interrupted"
REACT_HALT_KEY = "analysis_react_halt"

# ---------------------------------------------------------------------------
# Native tool schemas for the analysis ReAct loop.
# Passed to the LLM so it emits structured tool_use blocks. The loop's terminal
# condition is an assistant turn with no tool call — there is no final_answer tool.
# ---------------------------------------------------------------------------
_ANALYSIS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": (
                "Execute a Python script in the sandboxed session workspace. The active "
                "dataset is a parquet file whose exact name is given in the context "
                "(load it with `pd.read_parquet('<active dataset>.parquet')`). "
                "Imports of any installed library are allowed. Use print() to surface results. "
                "To change the working dataset, either overwrite the active file in place, or "
                "write a new file and call set_active_dataset to promote it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "path": {
                        "type": "string",
                        "description": "Optional: run a .py file already in the workspace instead of code",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the session workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text/CSV file from the session workspace (relative path).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "start_line": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a workspace file with text content.",
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
            "description": "Delete a file from the session workspace.",
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
            "name": "run_sql",
            "description": (
                "Run one or more read-only queries against the active analytical relation. "
                "SELECT, WITH, VALUES, and TABLE are allowed; leading comments are fine. "
                "Separate multiple queries with semicolons. Use the {table} placeholder "
                "for the safely quoted table name. Writes belong in sql_write_temp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "One or more semicolon-separated read-only SQL statements",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "Get column names, dtypes, null counts, and short samples of the active dataset.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_active_dataset",
            "description": (
                "Promote a workspace file (.parquet or .csv) to be the session's working "
                "dataset — what the app, charts, and the user see. Use after writing a new "
                "transformed file (e.g. an engineered dataset) you want to become canonical, "
                "instead of overwriting the original."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "needs_cleaning",
            "description": (
                "Request the data-cleaning sub-agent. Use when quality issues would make "
                "analysis misleading. Pauses until user approves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why cleaning is needed",
                    }
                },
                "required": ["reason"],
            },
        },
    },
]

# SQL sessions expose only the database tools — there is no on-disk workspace, so the
# file/script tools and the file-only needs_cleaning handoff are omitted. Writes and
# working-copy lifecycle go through the shared tools the cleaning agent also uses.
MONGO_PIPELINE_TOOL = {
    "type": "function",
    "function": {
        "name": "run_pipeline",
        "description": (
            "Run a read-only MongoDB aggregation pipeline against the attached live collection "
            "or active working copy named in the context, given as JSON: a list of stages, e.g. "
            '[{"$match": {"$expr": {"$gt": ["$qty", 2]}}}, '
            '{"$group": {"_id": "$sku", "n": {"$sum": 1}}}]. '
            "Nested fields use dotted paths (address.city). Every document has a unique "
            "_id. $out, $merge, $function, $accumulator and $where are refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pipeline": {
                    "type": "string",
                    "description": "A JSON array of aggregation stages",
                }
            },
            "required": ["pipeline"],
        },
    },
}

_MONGO_ANALYSIS_TOOLS = [t for t in _ANALYSIS_TOOLS if t["function"]["name"] in {"get_schema"}] + [
    MONGO_PIPELINE_TOOL
]

_MONGO_REACT_PROMPT = """You are a data analysis agent working against MongoDB.
At each step, decide the single best action to make progress toward answering the question.

The exact collection is given in the context below. It is either the attached LIVE source
(read-only) or the session's active private working copy after cleaning starts. Read-only
analysis never creates a clone. The user's source collection is never modified. There is NO
Python sandbox and no file workspace in this session.

Query it with aggregation pipelines through run_pipeline. Nested fields use dotted paths
(`address.city`). Every document has a unique, immutable `_id`.

Two things about this data model matter for how you answer:

- **Documents are not rows.** Fields may be absent from some documents entirely, which is
  different from being null. Use {"$ifNull": ["$field", null]} when a missing field should be
  treated as empty, and prefer $expr comparisons so missing values do not sort as smaller than
  numbers.
- **The field list is a sample, not a schema.** The context below lists fields seen in a sample
  of documents. A field may exist that is not listed. If a result looks like a field is missing
  everywhere, say so rather than concluding the data is empty.

$lookup, $unionWith, and $graphLookup are refused because other collections are not attached.
$out, $merge, $function, $accumulator and $where are refused: they write or execute code.
"""

_SQL_ANALYSIS_TOOLS = (
    [t for t in _ANALYSIS_TOOLS if t["function"]["name"] in {"run_sql", "get_schema"}]
    + [SQL_WRITE_TEMP_TOOL]
    + WORKING_COPY_TOOLS
)

_SQL_REACT_PROMPT = """You are a data analysis agent working against a PostgreSQL database.
At each step, decide the single best action to make progress toward answering the question.

The data lives in a PostgreSQL table whose exact name is given in the context below. This table
is the session's ACTIVE WORKING COPY (in the stych_temp schema) of the user's source table. It
is safe to modify, and its contents are exactly what the user sees in the app's data preview and
diff views. The user's original table is never touched by your changes. A session can hold
several named working copies (branches); the active one is what run_sql/sql_write_temp and the
{table} placeholder resolve to. There is NO Python sandbox and no file workspace in this session.

Tools:
- run_sql(query): Run one or more read-only queries (SELECT / WITH / VALUES / TABLE) against the
  table and see the result rows. Leading comments are fine. Separate multiple queries with
  semicolons — they run as a batch in one step. CTEs are allowed. Writes are rejected here; use
  sql_write_temp.
- sql_write_temp(sql): Apply UPDATE/ALTER/INSERT/DELETE statements to the active working copy. Use
  the {table} placeholder (or the full quoted table name). This is the ONLY way to persist
  changes — computing results with run_sql alone saves nothing. Never tell the user the data was
  modified unless a sql_write_temp call succeeded.
- get_schema(): Column names, Postgres data types, null counts and row count for the table.
- create_working_copy({name?, from?}): New named branch from the original table (default) or from
  an existing copy; it becomes active.
- switch_working_copy({name}) / drop_working_copy({name}) / list_working_copies(): Manage the
  session's copies. Drop only when the user explicitly asks to discard one.
- diff_working_copies({base?, target?}): Change summary between two versions ('source' or a copy
  name) — rows added/removed/changed and per-column changed cells. Its numbers match the user's
  Diff tab.

Notes on the data: some numeric-looking columns are stored as TEXT, and missing values may appear
as placeholder strings ('UNKNOWN', 'N/A', '-', 'nan', …) rather than NULL — handle both when
relevant. Guard casts so a bad value can't abort the query, e.g.
AVG(CASE WHEN "qty" ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN "qty"::numeric END). Use COALESCE for NULLs
and LIMIT to cap large result sets.

Plan your own approach — you decide what to look at, in what order, and when you are done. Each
LLM step is expensive: prefer fewer, richer steps (several semicolon-separated SELECTs in one
run_sql call, or many aggregates in one query) over many small ones. Don't modify the data unless
the task calls for it. When you have a complete answer, output it as plain text — do not call any
tool."""


def _sql_react_prompt(dialect) -> str:
    """The SQL analysis prompt for one engine.

    Previously there were two hardcoded variants, so every connector other than
    Databricks was told it was querying PostgreSQL — and wrote Postgres SQL
    against BigQuery, Snowflake and Redshift.
    """
    return _SQL_REACT_PROMPT_TEMPLATE.format(
        engine=dialect.display_name, syntax=dialect.syntax_notes
    )


_SQL_REACT_PROMPT_TEMPLATE = """You are a data analysis agent working against {engine}.
At each step, decide the single best action to make progress toward answering the question.

The context below says whether {{table}} resolves to the attached LIVE source (read-only) or an
ACTIVE WORKING COPY. Every read query MUST use the {{table}} placeholder; literal relation names,
joins to unattached relations, and credential-wide exploration are refused. sql_write_temp creates
a private scratch working copy lazily when the first mutation is requested, and never updates the
user's source table. There is NO Python sandbox and no file workspace in this session.

Write {engine}. {syntax}
Always use {{table}} — do not copy, shorten, re-quote, or substitute the displayed relation name.

"""

_DATABRICKS_SQL_REACT_PROMPT = """You are a data analysis agent working against Databricks SQL.
At each step, decide the single best action to make progress toward answering the question.

The data lives in a Databricks table whose exact name is given in the context below. This table
is the session's ACTIVE WORKING COPY in a Stych-owned scratch schema (default stych_temp), cloned
from the user's source table. It is safe to modify, and its contents are exactly what the user
sees in the app's data preview and diff views. NEVER UPDATE the user's source table. A session
can hold several named working copies (branches); the active one is what run_sql/sql_write_temp
and the {table} placeholder resolve to. There is NO Python sandbox and no file workspace.

Write Databricks SQL. Quote identifiers with backticks (`catalog`.`schema`.`table`). Use
CAST(x AS DOUBLE) / CAST(x AS STRING), RLIKE for regex, date_format / date_trunc for dates.
Do not use Postgres-only syntax (to_char, ::numeric, ~).

Tools:
- run_sql(query): Run one or more read-only queries (SELECT / WITH / VALUES / TABLE; pipe queries
  may start with FROM) against the table and see the result rows. Leading comments are fine.
  Separate multiple queries with semicolons — they run as a batch in one step. CTEs are allowed.
  Writes are rejected here; use sql_write_temp.
- sql_write_temp(sql): Apply UPDATE/ALTER/INSERT/DELETE statements to the active working copy. Use
  the {table} placeholder (or the full backtick-quoted name). This is the ONLY way to persist
  changes — computing results with run_sql alone saves nothing. Never tell the user the data was
  modified unless a sql_write_temp call succeeded.
- get_schema(): Column names, data types, null counts and row count for the table.
- create_working_copy({name?, from?}): New named branch from the original table (default) or from
  an existing copy; it becomes active.
- switch_working_copy({name}) / drop_working_copy({name}) / list_working_copies(): Manage the
  session's copies. Drop only when the user explicitly asks to discard one.
- diff_working_copies({base?, target?}): Change summary between two versions ('source' or a copy
  name) — rows added/removed/changed and per-column changed cells. Its numbers match the user's
  Diff tab.

Guard casts so a bad value can't abort the query, e.g.
AVG(CASE WHEN `qty` RLIKE '^-?[0-9]+(\\\\.[0-9]+)?$' THEN CAST(`qty` AS DOUBLE) END).
Use COALESCE for NULLs and LIMIT to cap large result sets.

Plan your own approach — you decide what to look at, in what order, and when you are done. Each
LLM step is expensive: prefer fewer, richer steps (several semicolon-separated SELECTs in one
run_sql call, or many aggregates in one query) over many small ones. Don't modify the data
unless the task calls for it. When you have a complete answer, output it as plain text — do not
call any tool."""


_DEFAULT_REACT_PROMPT = """You are a data analysis agent working in a sandboxed file workspace.
At each step, decide the single best action to make progress toward answering the question.

The active dataset lives in the workspace as a parquet file (canonical, correctly typed).
Its exact filename is given in the context below — use that name, do not assume `data.parquet`.
Each run_script call is a fresh process: load the data yourself with
`pd.read_parquet('<active dataset>.parquet')` at the top of every script.
To persist changes to the working dataset for later steps and the rest of the app, either
overwrite the active file in place (`df.to_parquet('<active dataset>.parquet', index=False)`)
or write a new file and call `set_active_dataset` to promote it. New files you create
(reports, intermediate CSVs) are kept and shown to the user as workspace artifacts.

Tools (file sessions use the workspace; SQL sessions use the database):
- run_script({{code}} or {{path}}): Run Python in the workspace. Any installed library may be
  imported (pandas, numpy, scipy, scikit-learn, statsmodels, …). Use print() to show results.
  Blocked for safety: os, sys, subprocess, socket, shutil, pathlib, requests, httpx (and the
  network is disabled). Read/write files only inside the workspace.
- list_files(): List workspace files.
- read_file({{path, start_line?, max_lines?}}): Read a text/CSV file.
- write_file({{path, content}}): Create/overwrite a text file (e.g. a report or a .py script).
- edit_file({{path, old_string, new_string}}): Replace an exact unique substring.
- delete_file({{path}}): Remove a workspace file.
- set_active_dataset({{path}}): Promote a workspace .parquet/.csv file to be the working dataset.
- get_schema(): Column names, dtypes, nulls, and short samples of the active dataset.
- run_sql(query): Run one or more read-only DuckDB queries over the active snapshot. Use {table}
  as the table. Leading comments and semicolon-separated batches are allowed.
- needs_cleaning({{reason}}): Request the data-cleaning sub-agent with user permission. Use when
  quality issues (missing, duplicates, type chaos) would make analysis misleading. Pauses the run
  until the user approves; do not use on SQL sessions.

Plan your own approach — you decide what to look at, in what order, and when you are done. Each
LLM step is expensive: prefer fewer, richer steps (one script can explore, compute, and verify in
a single run) over many small ones. When you have a complete answer, output it as plain text —
do not call any tool."""


class DataAnalysisAgent(BaseStychAgent):
    """Agent for data analysis and insights generation."""

    def __init__(
        self,
        llm_router: LLMRouter,
        sql_agent: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name="DataAnalysisAgent",
            description="Analyzes datasets and generates insights",
            llm_router=llm_router,
        )

        self._config = config or {}
        self.sql_agent = sql_agent  # Optional SQLAgent for Postgres-backed sessions
        self.csv_agent = None  # Set by orchestrator: CSVAgent for file-based sessions
        self.system_prompt = """You are a data analysis expert. Given a dataset, you can:
- Generate comprehensive statistical summaries
- Identify correlations and patterns
- Analyze distributions
- Provide actionable insights
- Answer questions about the data

Always be precise, use the available tools, and provide clear explanations."""

        self.tools = DataAnalysisTools()
        # Pass config so the internal cleaning agent (needs_cleaning handoff)
        # honours agents.use_unified_engine too.
        self.cleaning_agent = DataCleaningAgent(llm_router, config=self._config)
        # Shared, sandboxed workspace executor + file/script toolset (same model the
        # cleaning agent uses). Replaces the legacy AnalysisSandboxRunner.
        self.workspace_runner = WorkspaceSandboxRunner(timeout_seconds=60)
        self._toolset = WorkspaceToolset(self.workspace_runner)
        # react_tools is rebuilt in _execute_react_loop to pick up the sql_agent
        # reference that may be updated after __init__ (e.g. via orchestrator).
        self._react_tools_cache: Optional[ReactToolRegistry] = None
        self._workspace_manager = WorkspaceManager()
        logger.info(
            "[AnalysisAgent:init] initialized (timeout_seconds=%s)",
            self.workspace_runner.timeout_seconds,
        )

    def _log(
        self,
        event: str,
        session_id: Optional[str] = None,
        **fields: Any,
    ) -> None:
        parts = [f"[AnalysisAgent:{event}]"]
        if session_id:
            parts.append(f"session_id={session_id}")
        for key, value in fields.items():
            parts.append(f"{key}={value}")
        logger.info(" ".join(parts))

    def _session_workspace_path(self, state: Optional[AgentState]) -> Optional[Path]:
        if not state or not state.session_id:
            return None
        if state.data_context and state.data_context.is_sql_datasource():
            return None
        uid = state.metadata.get("user_id")
        root = self._workspace_manager.get_or_create(state.session_id, user_id=uid)
        # Ensure the active dataset exists (e.g. after a server restart/restore, or for
        # sessions created before workspace seeding). Re-seed from working_dataframe only
        # when no dataset file is present so we never clobber a previously-written one.
        try:
            if state.data_context is not None:
                if not state.metadata.get("active_dataset"):
                    state.metadata["active_dataset"] = dataset_stem(state.data_context.filename)
                # Seed every dataset (primary + additional) so the agent can read any of
                # them; active_dataset only marks the default working frame. Missing files
                # are (re)seeded — e.g. after a restart/restore — without clobbering existing
                # ones (which may hold prior cleaning).
                seed_all_datasets(root, state)
        except Exception as exc:  # noqa: BLE001 — best-effort
            self._log("workspace_seed:failed", session_id=state.session_id, error=str(exc))
        return root

    def _active_dataset_line(self, state: Optional[AgentState]) -> str:
        """One authoritative line naming the active dataset file, injected into the
        system prompt so the LLM loads/saves the real filename (not a generic alias)."""
        if not state or not state.data_context or state.data_context.is_sql_datasource():
            return ""
        stem = state.metadata.get("active_dataset") or dataset_stem(state.data_context.filename)
        line = (
            f"Active dataset file: `{stem}.parquet`. "
            f"In run_script, load it with pd.read_parquet('{stem}.parquet') and, to update the "
            f"working dataset, write back to that same file (or call set_active_dataset on a new one). "
            f"Name any new dataset file you create with a suffix indicating the change "
            f"(e.g. `{stem}_imputed.csv`, `{stem}_encoded.parquet`) — never a generic name like output.csv."
        )
        # Other uploaded datasets are also seeded as files in the same workspace. Name
        # them so the agent knows it can read/join any of them (not just the active one)
        # when the task spans multiple datasets.
        others = [
            dataset_stem(dc.filename)
            for dc in getattr(state, "additional_data_contexts", None) or []
            if dc is not None and not dc.is_sql_datasource()
        ]
        others = [s for s in others if s and s != stem]
        if others:
            listed = ", ".join(f"`{s}.parquet`" for s in others)
            line += (
                f" Other datasets are present in the workspace and can be read directly "
                f"(pd.read_parquet): {listed}. Use them when the task spans multiple datasets."
            )
        return line

    async def _build_sql_session_context(
        self, state: Optional[AgentState]
    ) -> Optional[Dict[str, Any]]:
        """For Postgres sessions, ensure the temp table exists and return the real
        table reference, column schema, and row count for the ReAct prompt/tools.

        Returns None for file-based sessions (or when no sql_agent is wired)."""
        if not state or not state.data_context or not state.data_context.is_sql_datasource():
            return None
        if (state.data_context.metadata or {}).get("source") == "mongodb":
            return await self._build_mongo_session_context(state)
        if not self.sql_agent:
            return None
        target = await self.sql_agent.describe_read_target(state)
        cols = target["columns"]
        row_count = target["row_count"]
        temp_ref = target["ref"]
        schema_lines = [
            f"  {c['column_name']} ({c['data_type']}, nullable: {c['is_nullable']})" for c in cols
        ]
        relation_note = (
            "This is the session's private working copy; cleaning changes are visible in preview/diff."
            if target["working_copy"]
            else "This is the attached LIVE source and is READ-ONLY. Use {table} in every query; "
            "a private working copy is created lazily only when cleaning starts."
        )
        schema_text = (
            f"Table: {temp_ref}\n{relation_note}\n"
            f"Row count: {row_count}\n"
            f"Columns:\n" + "\n".join(schema_lines)
        )
        return {
            "temp_schema": target["schema"],
            "temp_table": target["table"],
            "temp_ref": temp_ref,
            "row_count": row_count,
            "columns": cols,
            "schema_text": schema_text,
        }

    async def _build_mongo_session_context(self, state: AgentState) -> Dict[str, Any]:
        """Context for a MongoDB session.

        Deliberately says the field list is a *sample*: a collection has no
        declared schema, so a model that treats this as exhaustive will conclude
        a field is missing everywhere when it simply was not sampled.
        """
        import asyncio  # noqa: PLC0415

        from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415
        from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

        meta = state.data_context.metadata or {}
        collection = meta.get("collection") or meta.get("table")
        if not collection:
            raise MongoRunnerError("MongoDB session has no collection attached.")
        try:
            runner = MongoScratchRunner.from_meta(meta)
            from agents.sql_agent import working_copies as wc  # noqa: PLC0415

            wc.adopt_legacy(meta)
            active = wc.active_copy(meta)
            if active and await asyncio.to_thread(
                runner.table_exists, runner.temp_schema, active["table"]
            ):
                database, name = runner.temp_schema, active["table"]
                working_copy = True
            else:
                if active:
                    wc.remove_copy(meta, active["name"])
                database, name = str(meta.get("database") or ""), str(collection)
                working_copy = False
            columns = await asyncio.to_thread(runner.get_table_columns, database, name)
            row_count = await asyncio.to_thread(runner.get_row_count, database, name)
        except MongoRunnerError as exc:
            logger.warning("[analysis] MongoDB working copy unavailable: %s", exc)
            raise

        ref = runner.quote_relation(database, name)
        field_lines = [f"  {c['column_name']} ({c['data_type']})" for c in columns]
        schema_text = (
            f"Collection: {ref}\n"
            + (
                f"This is the session's private working copy of {collection!r}.\n"
                if working_copy
                else f"This is the attached LIVE collection {collection!r}, read-only. A private "
                "working copy is created lazily only when cleaning starts.\n"
            )
            + f"Document count: {row_count}\n"
            f"Fields seen in a sample of documents (NOT a complete schema — a field may "
            f"exist that is not listed):\n" + "\n".join(field_lines)
        )
        return {
            "temp_schema": database,
            "temp_table": name,
            "temp_ref": ref,
            "row_count": row_count,
            "columns": columns,
            "schema_text": schema_text,
            "working_copy": working_copy,
        }

    async def _build_duckdb_session_context(
        self, state: Optional[AgentState]
    ) -> Optional[Dict[str, Any]]:
        if not state or not state.data_context or state.data_context.is_sql_datasource():
            return None
        from execution.session_plane import PlaneKind, resolve_data_plane  # noqa: PLC0415

        plane = await asyncio.to_thread(resolve_data_plane, state, state.data_context)
        if plane.kind != PlaneKind.DUCKDB or plane.relation is None:
            return None
        columns = "\n".join(
            f"  {column.name} ({column.data_type})" for column in plane.relation.columns
        )
        return {
            "relation_ref": f'"{plane.relation_name}"',
            "row_count": plane.row_count,
            "scope": plane.scope.value,
            "row_limit": plane.row_limit,
            "truncated": plane.truncated,
            "schema_text": f'DuckDB relation: "{plane.relation_name}"\nColumns:\n{columns}',
        }

    @staticmethod
    def _postgres_lost_connection(state: AgentState) -> bool:
        """True for a Postgres-origin session whose connection_url is no longer
        usable — e.g. restored from a snapshot whose encrypted URL couldn't be
        decrypted. Such a session can't run SQL until the user reconnects."""
        dc = state.data_context
        return bool(dc and dc.metadata.get("source") == "postgres" and not dc.is_sql_datasource())

    def _postgres_reconnect_result(self, question: str) -> Dict[str, Any]:
        return {
            "status": STATUS_COMPLETED,
            "response": (
                "This analysis is backed by a PostgreSQL connection that is no longer "
                "available in this session (it was restored from a saved snapshot, and "
                "database credentials are not kept in snapshots). Please reconnect the "
                "table to continue analyzing the full dataset."
            ),
            "result_data": None,
            "metadata": {
                "execution_mode": "sql",
                "needs_reconnect": True,
                "original_question": question,
            },
        }

    def _maybe_reload_working_dataframe_from_artifact(self, state: AgentState) -> None:
        """Reload the canonical ``data.parquet`` into working_dataframe when a prior
        script (e.g. the cleaning handoff) wrote it. Gated on the
        ``workspace_canonical_file`` flag so unrelated mutations aren't clobbered."""
        path_s = state.metadata.get("workspace_canonical_file")
        if not path_s or not state.data_context:
            return
        if state.data_context.is_sql_datasource():
            return
        try:
            p = Path(path_s)
            if p.is_file():
                state.data_context.working_dataframe = pd.read_parquet(p, engine="pyarrow")
        except Exception as exc:  # noqa: BLE001
            self._log(
                "reload_canonical_artifact:failed",
                session_id=state.session_id,
                error=str(exc),
            )

    def _mark_user_cleaning_completed(self, state: AgentState) -> None:
        state.metadata["user_ran_cleaning"] = True
        state.metadata["readiness_cache_invalid"] = True
        p = state.metadata.get("workspace_canonical_file")
        append_handoff_event(
            state,
            source_agent="analysis",
            action="analysis_after_cleaning",
            details={
                "primary_artifact": p,
                "filename": state.data_context.filename if state.data_context else None,
            },
        )

    async def process_query(
        self,
        state: AgentState,
        message: str,
        user_action: Optional[Dict[str, Any]] = None,
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        """
        Main analysis workflow:
        1. Resume a pending HITL (e.g. needs_cleaning tool consent) if present
        2. For file data, ReAct (tools include optional needs_cleaning handoff to cleaning)
        3. For SQL-only, delegate to the SQL analysis path
        """
        self._log(
            "process_query:start",
            session_id=state.session_id,
            message_preview=message[:120],
            has_data_context=state.data_context is not None,
            has_pending=bool(state.metadata.get(PENDING_KEY)),
            has_user_action=user_action is not None,
        )

        async def _emit(msg: str) -> None:
            if progress_emitter:
                await progress_emitter.emit("analysis", msg)

        async def _emit_reasoning(
            *,
            step: int,
            phase: str,
            thought: str,
            action: str,
            tool_input: Any = "",
            observation: Any = "",
        ) -> None:
            if progress_emitter:
                await progress_emitter.emit(
                    "analysis",
                    "Reasoning",
                    event_type=SseEventType.REASONING.value,
                    step=step,
                    phase=phase,
                    thought=thought or "",
                    action=action or "",
                    tool_input=preview(tool_input, 500),
                    observation=preview(observation, 1200) if observation else "",
                )

        if not state.data_context:
            self._log("process_query:no_data_context", session_id=state.session_id)
            return {
                "status": STATUS_COMPLETED,
                "response": "Please upload a dataset first to analyze.",
                "metadata": {},
            }

        pending = state.metadata.get(PENDING_KEY)
        if pending:
            self._log(
                "process_query:pending_detected",
                session_id=state.session_id,
                pending_type=pending.get("type"),
            )
            return await self._handle_pending_cleaning_step(
                state=state,
                message=message,
                user_action=user_action,
                pending=pending,
                progress_emitter=progress_emitter,
            )

        self._maybe_reload_working_dataframe_from_artifact(state)
        working_df = state.data_context.working_dataframe

        self._log(
            "process_query:executing_analysis",
            session_id=state.session_id,
            rows=len(working_df),
            columns=len(working_df.columns),
        )
        source = (state.data_context.metadata or {}).get("source")
        if state.data_context.is_sql_datasource() and (self.sql_agent or source == "mongodb"):
            # Warehouse/Mongo sessions stay on the source-side ReAct loop even if
            # Mongo has no SQLAgent wired. Falling through would use file-workspace
            # tools against a preview frame.
            return await self._execute_analysis(
                working_df=working_df,
                question=message,
                state=state,
                note_prefix="",
                progress_emitter=progress_emitter,
            )
        if self._postgres_lost_connection(state):
            return self._postgres_reconnect_result(message)
        if self.csv_agent:
            return await self.csv_agent.run_analysis(
                state=state,
                question=message,
                note_prefix="",
                progress_emitter=progress_emitter,
            )
        return await self._execute_analysis(
            working_df=working_df,
            question=message,
            state=state,
            note_prefix="",
            progress_emitter=progress_emitter,
        )

    async def analyze(
        self,
        dataframe: pd.DataFrame,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        High-level descriptive analysis path (kept for compatibility).
        """
        try:
            self._log(
                "analyze:start",
                session_id=None,
                query_present=bool(query),
                rows=len(dataframe),
                columns=len(dataframe.columns),
            )
            stats = self.tools.describe_statistics(dataframe)
            correlations = self.tools.correlation_analysis(dataframe)
            missing_report = self.tools.missing_values_report(dataframe)

            context = {
                "statistics": stats,
                "correlations": correlations,
                "missing_values": missing_report,
            }

            messages = [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": f"""Analyze this dataset and provide insights:

Dataset Info:
- Shape: {stats.get('shape', {})}
- Columns: {', '.join(stats.get('columns', []))}

Key Statistics:
{context}

"""
                    + (
                        f"Specific Question: {query}"
                        if query
                        else "Provide a comprehensive analysis with key insights."
                    ),
                },
            ]

            insights = await self.generate_response(messages)
            self._log("analyze:completed", session_id=None)
            return {
                "type": "analysis",
                "statistics": stats,
                "correlations": correlations,
                "missing_values": missing_report,
                "insights": insights,
            }

        except Exception as e:
            logger.error(f"Error in data analysis: {e}")
            return {"error": str(e)}

    async def answer_question(
        self,
        dataframe: pd.DataFrame,
        question: str,
    ) -> str:
        """Compatibility path used by direct analysis endpoint."""
        try:
            self._log(
                "answer_question:start",
                session_id=None,
                question_preview=question[:120],
                rows=len(dataframe),
                columns=len(dataframe.columns),
            )
            result = await self._execute_analysis(
                working_df=dataframe,
                question=question,
                state=None,
                note_prefix="",
            )
            return result.get("response", "I could not generate an answer.")
        except Exception as e:
            logger.error(f"Error answering question: {e}")
            self._log(
                "answer_question:fallback_without_execution",
                session_id=None,
                error=str(e),
            )
            stats = self.tools.describe_statistics(dataframe)
            return await self._answer_without_execution(dataframe, question, stats)

    async def assess_readiness(
        self,
        working_df: pd.DataFrame,
        question: str,
    ) -> Dict[str, Any]:
        """Determine whether cleaning consent is needed before analysis."""
        self._log(
            "assess_readiness:start",
            session_id=None,
            question_preview=question[:120],
            rows=len(working_df),
            columns=len(working_df.columns),
        )
        quality_summary = self._build_quality_summary(working_df)
        deterministic_signal = self._deterministic_cleaning_signal(quality_summary, question)
        quality_summary["deterministic_signal"] = deterministic_signal
        llm_cleaning_needed = False  # default; overwritten when LLM is consulted
        if not deterministic_signal:
            # No quality issues detected — skip the LLM call entirely.
            cleaning_needed = False
            llm_reason = "deterministic check: no issues"
        else:
            llm_cleaning_needed, llm_reason = await self._llm_cleaning_judgement(
                quality_summary, question
            )
            cleaning_needed = llm_cleaning_needed
        self._log(
            "assess_readiness:quality_decision",
            session_id=None,
            deterministic_signal=deterministic_signal,
            llm_cleaning_needed=llm_cleaning_needed,
            cleaning_needed=cleaning_needed,
        )
        if cleaning_needed:
            return {
                "status": READINESS_NEEDS_CLEANING,
                "quality_summary": {
                    **quality_summary,
                    "llm_reason": llm_reason,
                },
            }
        return {
            "status": READINESS_READY,
            "quality_summary": quality_summary,
        }

    async def _handle_pending_cleaning_step(
        self,
        state: AgentState,
        message: str,
        user_action: Optional[Dict[str, Any]],
        pending: Dict[str, Any],
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        self._log(
            "pending_cleaning:start",
            session_id=state.session_id,
            pending_type=pending.get("type"),
            has_user_action=user_action is not None,
            message_preview=message[:120],
        )
        ptype = pending.get("type")

        prompt_id = pending.get("prompt_id")
        decision = self._parse_cleaning_decision(
            message=message,
            user_action=user_action,
            expected_prompt_id=prompt_id,
        )
        if decision is None and not user_action:
            decision = await self._llm_parse_cleaning_decision(message)

        if decision is None:
            quality = pending.get("quality_summary", {}) or {}
            action_prompt = self._build_cleaning_consent_prompt(quality, prompt_id=prompt_id)
            self._log(
                "pending_cleaning:awaiting_valid_response",
                session_id=state.session_id,
                prompt_id=prompt_id,
            )
            return {
                "status": STATUS_AWAITING_USER_ACTION,
                "response": action_prompt["question"],
                "action_prompt": action_prompt,
                "metadata": {
                    "readiness_status": READINESS_NEEDS_CLEANING,
                    "quality_summary": quality,
                    "pending_kind": ptype,
                },
            }

        state.metadata.pop(PENDING_KEY, None)
        self._log(
            "pending_cleaning:decision_received",
            session_id=state.session_id,
            decision=decision,
            prompt_id=prompt_id,
        )

        # Every needs-cleaning pause is stored with engine=True; run the cleaning
        # handoff, then resume the engine from its persisted transcript.
        return await self._handle_pending_engine(
            state=state,
            decision=bool(decision),
            pending=pending,
            user_action=user_action,
            progress_emitter=progress_emitter,
        )

    async def _handle_pending_engine(
        self,
        state: AgentState,
        decision: bool,
        pending: Dict[str, Any],
        user_action: Optional[Dict[str, Any]],
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        """
        Resume a unified-engine analysis loop that paused on ``needs_cleaning``.

        Approve  → run the cleaning sub-agent, then resume the engine with the
                   outcome as the paused tool's result.
        Decline  → resume the engine telling it to continue with the current df.
        If cleaning itself needs HITL, stash the engine pending under
        ANALYSIS_REACT_INTERRUPTED_KEY and let resume_react_after_interrupted_cleaning
        pick it up.
        """
        engine_pending = pending.get("engine_pending")
        original_question = str(pending.get("original_question", ""))
        halt_reason = pending.get("halt_reason", "")
        is_sql = bool(state.data_context and state.data_context.is_sql_datasource())

        if decision and not is_sql:
            clean_msg = (
                f"Clean the working dataset. Issues flagged during analysis: "
                f"{halt_reason or '(see data)'}\n\n"
                f"User's analysis question to answer after cleaning: {original_question}"
            )
            cleaned = await self.cleaning_agent.process_query(
                state=state,
                message=clean_msg,
                user_action=user_action,
                progress_emitter=progress_emitter,
            )
            if cleaned.get("status") == STATUS_AWAITING_USER_ACTION:
                state.metadata[ANALYSIS_REACT_INTERRUPTED_KEY] = {
                    "engine": True,
                    "engine_pending": engine_pending,
                    "original_question": original_question,
                }
                return cleaned
            if cleaned.get("status") == STATUS_COMPLETED:
                self._maybe_reload_working_dataframe_from_artifact(state)
                self._mark_user_cleaning_completed(state)
                obs = (
                    "Cleaning completed. The working dataset was refreshed from workspace "
                    "output when a cleaned artifact is present. Continue the analysis."
                )
            else:
                obs = (
                    "Cleaning did not finish in a success state. Proceed with the current "
                    "working data."
                )
            return await self._resume_unified_loop(
                state, engine_pending, obs, original_question, progress_emitter
            )

        # Declined (or SQL session where needs_cleaning is unavailable): continue as-is.
        obs = (
            "The user asked to continue without a dedicated cleaning pass. Use the current "
            "`df` as-is for the next steps."
        )
        return await self._resume_unified_loop(
            state, engine_pending, obs, original_question, progress_emitter
        )

    async def _execute_analysis(
        self,
        working_df: pd.DataFrame,
        question: str,
        state: Optional[AgentState],
        note_prefix: str,
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        """Entry point — delegates to the ReAct loop."""
        return await self._execute_react_loop(
            working_df=working_df,
            question=question,
            state=state,
            note_prefix=note_prefix,
            progress_emitter=progress_emitter,
        )

    async def resume_react_after_interrupted_cleaning(
        self,
        state: AgentState,
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        """
        After the cleaning sub-agent finishes an interactive (HITL) flow that was
        started from analysis' ``needs_cleaning`` tool, continue the analysis ReAct loop.
        """
        ctx = state.metadata.pop(ANALYSIS_REACT_INTERRUPTED_KEY, None)
        if not ctx or not state.data_context:
            return {
                "status": STATUS_ERROR,
                "response": "Could not resume analysis: missing interrupted state.",
                "metadata": {},
            }
        self._maybe_reload_working_dataframe_from_artifact(state)
        self._mark_user_cleaning_completed(state)

        # The interrupted context is always stored with engine=True; continue the
        # persisted transcript.
        return await self._resume_unified_loop(
            state,
            ctx.get("engine_pending"),
            "Cleaning (including any follow-up questions) has finished and the working "
            "dataset was refreshed when available. Continue the original analysis.",
            str(ctx.get("original_question") or ""),
            progress_emitter,
        )

    # ------------------------------------------------------------------
    # Unified-engine path (agents.use_unified_engine = true)
    # ------------------------------------------------------------------

    def _unified_loop_setup(
        self,
        working_df: pd.DataFrame,
        question: str,
        state: Optional[AgentState],
        note_prefix: str,
        progress_emitter: Optional[ProgressEmitter],
        sql_ctx: Optional[Dict[str, Any]] = None,
        duckdb_ctx: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build the shared pieces (tools, executor, finalizer, emitter, transcript)
        used by both a fresh unified run and a HITL resume. The ``needs_cleaning``
        tool raises ``PauseSignal``; everything else routes to ReactToolRegistry.

        ``df_holder`` carries the working frame; after each tool call it is refreshed
        from working_dataframe so run_script mutations (synced from data.parquet)
        propagate to get_schema/needs_cleaning across steps.
        """
        analysis_cfg = self._config.get("analysis", {})
        source = (
            (state.data_context.metadata or {}).get("source")
            if state and state.data_context
            else None
        )
        if source == "mongodb" and sql_ctx is None:
            from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

            raise MongoRunnerError(
                "MongoDB session has no analysis context; refusing file-workspace fallback."
            )
        is_sql = sql_ctx is not None
        wroot: Optional[Path] = (
            None if is_sql else (self._session_workspace_path(state) if state else None)
        )
        react_tools = ReactToolRegistry(
            self._toolset,
            self.sql_agent,
            workspace_root=wroot,
            quality_summary_fn=self._build_quality_summary,
            sql_schema_text=sql_ctx["schema_text"] if is_sql else None,
        )
        df_holder = {"df": working_df}
        pause_ctx: Dict[str, Any] = {}

        if is_sql:
            from datasources.sql_dialect import get_dialect  # noqa: PLC0415

            if source == "mongodb":
                # Documents, not rows: a different tool and a different prompt,
                # not a SQL dialect variation.
                tools = _MONGO_ANALYSIS_TOOLS
                dataset_mode = "MongoDB (attached collection)"
                react_prompt = analysis_cfg.get("mongo_react_agent_prompt") or _MONGO_REACT_PROMPT
            else:
                tools = _SQL_ANALYSIS_TOOLS
                dialect = get_dialect(source)
                dataset_mode = f"SQL ({dialect.display_name} scratch table)"
                react_prompt = analysis_cfg.get("sql_react_agent_prompt") or _sql_react_prompt(
                    dialect
                )
            # Fallback mirrors config/routing_config.yaml (analysis.sql_react_max_steps).
            max_steps = int(analysis_cfg.get("sql_react_max_steps", 12))
            system_content = f"{react_prompt}\n\n{sql_ctx['schema_text']}"
        else:
            tools = _ANALYSIS_TOOLS
            dataset_mode = (
                "cleaned working dataset"
                if state
                and state.data_context is not None
                and not state.data_context.raw_dataframe.equals(
                    state.data_context.working_dataframe
                )
                else "raw dataset"
            )
            react_prompt = analysis_cfg.get("react_agent_prompt") or _DEFAULT_REACT_PROMPT
            # Fallback mirrors config/routing_config.yaml (analysis.react_max_steps).
            max_steps = int(analysis_cfg.get("react_max_steps", 30))
            schema_summary = "\n".join(
                f"  {col} ({dtype})" for col, dtype in working_df.dtypes.astype(str).items()
            )
            active_line = self._active_dataset_line(state)
            system_content = (
                f"{react_prompt}\n\n"
                + (f"{active_line}\n" if active_line else "")
                + (
                    f"DuckDB analytical relation: {duckdb_ctx['relation_ref']} "
                    f"({duckdb_ctx['scope']} snapshot, {duckdb_ctx['row_count']} rows). "
                    "Use run_sql with the {table} placeholder for relational analysis.\n"
                    if duckdb_ctx
                    else ""
                )
                + f"Dataset mode: {dataset_mode}\n"
                f"Dataset shape: {working_df.shape[0]} rows × {working_df.shape[1]} columns\n"
                f"Columns:\n{schema_summary}"
            )

        async def _tool_executor(name: str, args: Any, _call_id: str) -> str:
            if name == "needs_cleaning":
                obs = self._unified_needs_cleaning(args, df_holder["df"], state, pause_ctx)
                if obs is not None:
                    return obs  # guard tripped — not a real pause
                raise PauseSignal(pause_ctx["action_prompt"], response=pause_ctx["question"])
            obs = await react_tools.call(
                tool_name=name, tool_input=args, df=df_holder["df"], state=state
            )
            # run_script may have rewritten data.parquet → the toolset synced it into
            # working_dataframe. Refresh the local frame so get_schema/needs_cleaning
            # see the latest data (no more silent `result`-as-df clobber).
            if state and state.data_context is not None:
                df_holder["df"] = state.data_context.working_dataframe
            return obs

        async def _emitter(rec: ReactStepRecord) -> None:
            await emit_react_part(
                progress_emitter, "analysis", rec, parent_id=_active_parent_part_id.get()
            )

        async def _finalizer(text: str, _transcript: List[Message]) -> Dict[str, Any]:
            response = f"{note_prefix}{text}".strip()
            meta: Dict[str, Any] = {
                "execution_mode": "react",
                "dataset_mode": dataset_mode,
                "original_question": question,
            }
            if state:
                meta.update(handoff_to_response_metadata(state))
            return {
                "status": STATUS_COMPLETED,
                "response": response,
                "result_data": text,
                "metadata": meta,
            }

        async def _budget_summarizer(transcript: List[Message]) -> str:
            trace = [
                {
                    "step": i + 1,
                    "action": m.role,
                    "observation": m.content or "",
                }
                for i, m in enumerate(transcript)
                if m.role == "tool"
            ]
            summary = await self._summarize_react_trace(question, trace, dataset_mode)
            return f"{note_prefix}{summary}".strip()

        toolkit = Toolkit.from_schemas(
            tools,
            lambda name, args: _tool_executor(name, args, ""),
            known_names=ReactToolRegistry.TOOL_NAMES,
            hitl={"needs_cleaning"},
            parallel_safe={"get_schema", "list_files", "read_file"},
        )

        return {
            "toolkit": toolkit,
            "emitter": _emitter,
            "finalizer": _finalizer,
            "budget_summarizer": _budget_summarizer,
            "system_content": system_content,
            "dataset_mode": dataset_mode,
            "pause_ctx": pause_ctx,
            "max_steps": max_steps,
        }

    def _unified_needs_cleaning(
        self,
        args: Any,
        df: pd.DataFrame,
        state: Optional[AgentState],
        pause_ctx: Dict[str, Any],
    ) -> Optional[str]:
        """
        Validate a needs_cleaning request. Returns an observation string when a
        guard trips (so the loop continues), or None to signal a genuine pause —
        in which case it populates ``pause_ctx`` with the consent prompt.
        """
        if state is None:
            return "ERROR: needs_cleaning requires a live session with state."
        if state.data_context and state.data_context.is_sql_datasource():
            return (
                "ERROR: needs_cleaning is for file-based datasets. For SQL sessions, use the "
                "dedicated data-cleaning path, then return here."
            )
        if state.metadata.get("user_ran_cleaning"):
            return (
                "The working dataset was already cleaned in this session. Do not request cleaning "
                "again unless the user explicitly asked. Use the current `df`."
            )
        reason = (
            str((args or {}).get("reason") or (args or {}).get("message"))
            if isinstance(args, dict)
            else str(args or "Data quality issues detected during analysis.")
        )
        quality = self._build_quality_summary(df)
        action_prompt = self._build_cleaning_consent_prompt(quality)
        pause_ctx.update(
            {
                "reason": reason,
                "quality_summary": quality,
                "prompt_id": action_prompt["prompt_id"],
                "action_prompt": action_prompt,
                "question": action_prompt["question"],
            }
        )
        return None

    async def _execute_unified_loop(
        self,
        working_df: pd.DataFrame,
        question: str,
        state: Optional[AgentState],
        note_prefix: str,
        progress_emitter: Optional[ProgressEmitter] = None,
        sql_ctx: Optional[Dict[str, Any]] = None,
        duckdb_ctx: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fresh unified-engine analysis run."""
        setup = self._unified_loop_setup(
            working_df,
            question,
            state,
            note_prefix,
            progress_emitter,
            sql_ctx=sql_ctx,
            duckdb_ctx=duckdb_ctx,
        )
        transcript = [
            Message(role="system", content=setup["system_content"]),
            Message(role="user", content=question),
        ]
        result = await run_agent_loop(
            llm_router=self.llm_router,
            agent_name="analysis",
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
        return self._finish_unified(result, state, question, setup["pause_ctx"])

    async def _resume_unified_loop(
        self,
        state: AgentState,
        engine_pending: Dict[str, Any],
        resume_observation: str,
        original_question: str,
        progress_emitter: Optional[ProgressEmitter] = None,
        note_prefix: str = "",
    ) -> Dict[str, Any]:
        """Resume a paused unified analysis loop after the cleaning handoff."""
        from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

        working_df = state.data_context.working_dataframe if state.data_context else None
        try:
            sql_ctx = await self._build_sql_session_context(state)
        except MongoRunnerError as exc:
            return self._mongo_context_error(exc)
        # File-session HITL must keep workspace tools; do not rebuild a DuckDB
        # context on resume. Mongo/SQL sessions still need sql_ctx so they do
        # not fall through to run_script.
        setup = self._unified_loop_setup(
            working_df,
            original_question,
            state,
            note_prefix,
            progress_emitter,
            sql_ctx=sql_ctx,
            duckdb_ctx=None,
        )
        result = await run_agent_loop(
            llm_router=self.llm_router,
            agent_name="analysis",
            toolkit=setup["toolkit"],
            transcript=[],
            max_steps=setup["max_steps"],
            temperature=0.5,
            output_token_cap=output_cap_from_config(self._config),
            transcript_token_budget=transcript_budget_from_config(self._config),
            emitter=setup["emitter"],
            finalizer=setup["finalizer"],
            budget_summarizer=setup["budget_summarizer"],
            resume=engine_pending,
            resume_observation=resume_observation,
        )
        return self._finish_unified(result, state, original_question, setup["pause_ctx"])

    def _finish_unified(
        self,
        result: Dict[str, Any],
        state: Optional[AgentState],
        question: str,
        pause_ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Map an engine result into the analysis-agent result contract."""
        status = result.get("status")
        if status == "awaiting_user_action":
            # Persist the engine's pending record plus analysis-specific context
            # so _handle_pending_cleaning_step can run the cleaning handoff and
            # resume this loop.
            if state is not None:
                state.metadata[PENDING_KEY] = {
                    "type": PENDING_TYPE_TOOL_NEEDS_CLEANING,
                    "engine": True,
                    # run_agent_loop(resume=...) expects both keys.
                    "engine_pending": {
                        "transcript": result["metadata"]["transcript"],
                        "pending": result["metadata"]["pending"],
                    },
                    "prompt_id": pause_ctx.get("prompt_id"),
                    "original_question": question,
                    "halt_reason": pause_ctx.get("reason", ""),
                    "quality_summary": pause_ctx.get("quality_summary", {}),
                }
            meta = {
                "execution_mode": "react",
                "original_question": question,
                "pending_tool": "needs_cleaning",
            }
            if state:
                meta.update(handoff_to_response_metadata(state))
            return {
                "status": STATUS_AWAITING_USER_ACTION,
                "response": result.get("response", pause_ctx.get("question", "")),
                "action_prompt": pause_ctx.get("action_prompt"),
                "metadata": meta,
            }
        # completed / partial both surface as a finished answer.
        return {
            "status": STATUS_COMPLETED,
            "response": result.get("response", ""),
            "result_data": result.get("response", ""),
            "metadata": result.get("metadata", {}),
        }

    async def _analysis_source_contexts(
        self, state: Optional[AgentState]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        sql_ctx = await self._build_sql_session_context(state)
        duckdb_ctx = (
            None if sql_ctx is not None else await self._build_duckdb_session_context(state)
        )
        return sql_ctx, duckdb_ctx

    def _mongo_context_error(self, exc: Exception) -> Dict[str, Any]:
        return {
            "status": STATUS_ERROR,
            "response": f"Could not reach the attached MongoDB collection. {exc}",
            "metadata": {"execution_mode": "react", "source": "mongodb"},
        }

    async def _execute_react_loop(
        self,
        working_df: pd.DataFrame,
        question: str,
        state: Optional[AgentState],
        note_prefix: str,
        progress_emitter: Optional[ProgressEmitter] = None,
    ) -> Dict[str, Any]:
        """
        ReAct loop: Thought → Action (tool call) → Observation → repeat.

        The LLM decides at each step which tool to call next (or to return a
        final answer). The loop is bounded by react_max_steps from config
        (default 6) to cap latency and cost.
        """
        from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

        # Postgres sessions ensure the temp table once and run the same loop with the
        # SQL tool subset (run_sql/get_schema) against the real table schema.
        try:
            sql_ctx, duckdb_ctx = await self._analysis_source_contexts(state)
        except MongoRunnerError as exc:
            return self._mongo_context_error(exc)

        return await self._execute_unified_loop(
            working_df=working_df,
            question=question,
            state=state,
            note_prefix=note_prefix,
            progress_emitter=progress_emitter,
            sql_ctx=sql_ctx,
            duckdb_ctx=duckdb_ctx,
        )

    async def _summarize_react_trace(
        self,
        question: str,
        trace: List[Dict[str, Any]],
        dataset_mode: str,
    ) -> str:
        """Synthesise a user-facing answer from the accumulated ReAct trace."""
        observations = "\n\n".join(
            f"Step {t['step']} ({t['action']}):\n{str(t.get('observation', ''))[:600]}"
            for t in trace
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "system",
                "content": f"You are summarising the results of a multi-step analysis on a {dataset_mode}.",
            },
            {
                "role": "user",
                "content": (
                    f"Original question: {question}\n\n"
                    f"Analysis steps and observations:\n{observations}\n\n"
                    "Synthesise a clear, concise answer based on the observations above. "
                    "Do NOT include code blocks."
                ),
            },
        ]
        return await self.generate_response(messages, temperature=0.2)

    def _build_code_gen_context(self, dataframe: pd.DataFrame) -> str:
        """Build a rich data context string for the code-generation prompt."""
        dtypes = {col: str(dtype) for col, dtype in dataframe.dtypes.items()}
        missing = dataframe.isnull().sum()
        missing_pct = (missing / len(dataframe) * 100).round(1)

        col_info_lines = []
        for col in dataframe.columns:
            dtype = dtypes[col]
            null_pct = missing_pct[col]
            uniq = dataframe[col].nunique(dropna=True)
            notes = []
            if null_pct > 0:
                notes.append(f"{null_pct}% missing")
            if uniq <= 15 and pd.api.types.is_object_dtype(dataframe[col]):
                sample_vals = dataframe[col].dropna().unique()[:8].tolist()
                notes.append(f"values: {sample_vals}")
            elif uniq <= 5:
                sample_vals = dataframe[col].dropna().unique()[:5].tolist()
                notes.append(f"values: {sample_vals}")
            col_info_lines.append(
                f"  {col} ({dtype}, {uniq} unique)" + (f" — {', '.join(notes)}" if notes else "")
            )

        preview_df = dataframe.head(min(3, len(dataframe))).copy()
        preview_records = preview_df.to_dict(orient="records")

        return (
            "Column details:\n"
            + "\n".join(col_info_lines)
            + f"\n\nPreview rows (up to 3):\n{json.dumps(preview_records, default=str)}"
        )

    async def _generate_analysis_code(
        self, dataframe: pd.DataFrame, question: str, dataset_mode: str
    ) -> str:
        stats = self.tools.describe_statistics(dataframe)
        data_context = self._build_code_gen_context(dataframe)
        self._log(
            "generate_analysis_code:start",
            session_id=None,
            dataset_mode=dataset_mode,
            question_preview=question[:120],
        )
        code_gen_messages = [
            {
                "role": "system",
                "content": """You are a Python data analysis code generator.
Generate ONLY executable Python code that answers the user's question.

Rules:
1. The dataframe is available as variable 'df'. pd, np, math, statistics are already imported — do NOT write any import statements.
2. Do not use file I/O or network calls. Do NOT use matplotlib, seaborn, or any plotting library.
3. Put the final answer in variable 'result'.
4. Return code only — no markdown fences, no comments, no explanations.
5. For string filters, prefer robust matching (case-insensitive, strip) when appropriate.
6. If user wording is approximate (e.g. "warehouse 2"), map to the closest real dataset value based on the column details below.
7. For numeric aggregations (mean, sum, etc.) always use pd.to_numeric(df[col], errors='coerce') before aggregating.
8. For date/time operations use pd.to_datetime(df[col], errors='coerce') before calling .dt accessors.
9. Exclude NaN rows from counts/aggregations by default.
10. Pandas 2.x offset aliases — ALWAYS use the NEW aliases or you will get a ValueError:
    - Monthly period end : 'ME'  (NOT 'M')
    - Year end           : 'YE'  (NOT 'Y' or 'A')
    - Business month end : 'BME' (NOT 'BM')
    - Week               : 'W'   (unchanged)
    - Day                : 'D'   (unchanged)
    - Hour               : 'h'   (NOT 'H')
    - Minute             : 'min' (NOT 'T')
    - Second             : 's'   (NOT 'S')""",
            },
            {
                "role": "system",
                "content": (
                    f"Dataset mode: {dataset_mode}\n"
                    f"Shape: {stats.get('shape', {})}\n" + data_context
                ),
            },
            {
                "role": "user",
                "content": f"Generate Python code to answer: {question}",
            },
        ]
        code = await self.generate_response(code_gen_messages)
        cleaned = self._clean_generated_code(code)
        self._log(
            "generate_analysis_code:completed",
            session_id=None,
            code_length=len(cleaned),
        )
        return cleaned

    async def _fix_analysis_code(
        self,
        dataframe: pd.DataFrame,
        question: str,
        broken_code: str,
        error: str,
        dataset_mode: str,
    ) -> str:
        """
        Ask the LLM to fix a code snippet that failed in the sandbox.
        Returns cleaned, corrected Python code.
        """
        data_context = self._build_code_gen_context(dataframe)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a Python debugging expert. "
                    "Fix the code below so it runs without errors and answers the user's question. "
                    "The dataframe is available as 'df'. pd, np, math, statistics are pre-imported. "
                    "Do NOT add import statements. Store the final answer in 'result'. "
                    "Return ONLY the corrected code — no markdown fences, no explanation."
                ),
            },
            {
                "role": "system",
                "content": f"Dataset mode: {dataset_mode}\n{data_context}",
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Broken code:\n```python\n{broken_code}\n```\n\n"
                    f"Error:\n{error}\n\nPlease fix the code."
                ),
            },
        ]
        fixed = await self.generate_response(messages)
        return self._clean_generated_code(fixed)

    def _clean_generated_code(self, code: str) -> str:
        cleaned = code.strip()
        if cleaned.startswith("```python"):
            cleaned = cleaned[9:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        # Strip import statements — pd, np, math, statistics are already
        # injected into the sandbox globals_dict so explicit imports fail
        # with "ImportError: __import__ not found" in the restricted exec env.
        lines = []
        for line in cleaned.splitlines():
            stripped_line = line.strip()
            if stripped_line.startswith("import ") or stripped_line.startswith("from "):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _build_quality_summary(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Compute data quality signals.

        All thresholds are read from config (quality section) so they can be
        tuned without code changes. Results are passed as context to the LLM
        cleaning judgement — not used as hard gates.
        """
        qcfg = self._quality_config()
        dtype_parse_ratio: float = float(qcfg.get("dtype_mismatch_parse_ratio", 0.80))
        outlier_zscore: float = float(qcfg.get("outlier_zscore_threshold", 3.0))
        outlier_ratio_thresh: float = float(qcfg.get("outlier_ratio_threshold", 0.05))
        outlier_min_sample: int = int(qcfg.get("outlier_min_sample", 30))

        row_count = len(df) if len(df) > 0 else 1
        missing_count = int(df.isnull().sum().sum())
        duplicate_count = int(df.duplicated().sum())
        numeric_columns = df.select_dtypes(include=["number"]).columns.tolist()
        object_columns = df.select_dtypes(include=["object"]).columns.tolist()

        dtype_mismatch_columns = []
        for col in object_columns:
            series = df[col].dropna()
            if series.empty:
                continue
            # Guard: if values look like integers/floats by type, skip
            if pd.api.types.is_integer_dtype(series) or pd.api.types.is_float_dtype(series):
                continue
            numeric_parse = pd.to_numeric(series, errors="coerce")
            parse_ratio = float(numeric_parse.notna().mean())
            if parse_ratio > dtype_parse_ratio:
                dtype_mismatch_columns.append(col)

        outlier_columns = []
        for col in numeric_columns:
            series = df[col].dropna()
            if len(series) < outlier_min_sample:
                continue
            std = series.std()
            if std == 0 or pd.isna(std):
                continue
            zscores = ((series - series.mean()) / std).abs()
            outlier_ratio = float((zscores > outlier_zscore).mean())
            if outlier_ratio > outlier_ratio_thresh:
                outlier_columns.append({"column": col, "outlier_ratio": round(outlier_ratio, 4)})

        return {
            "rows": int(len(df)),
            "columns": int(len(df.columns)),
            "missing_count": missing_count,
            "missing_ratio": missing_count / (row_count * max(1, len(df.columns))),
            "duplicate_count": duplicate_count,
            "duplicate_ratio": duplicate_count / row_count,
            "dtype_mismatch_columns": dtype_mismatch_columns,
            "outlier_columns": outlier_columns,
        }

    def _quality_config(self) -> Dict[str, Any]:
        return self._config.get("quality", {})

    def _deterministic_cleaning_signal(self, quality: Dict[str, Any], question: str) -> bool:
        """
        Return True when heuristics suggest cleaning *might* be needed.

        This is a *signal* fed to _llm_cleaning_judgement — it no longer gates
        the consent prompt by itself. Thresholds are read from config so they
        can be tuned without code changes.
        """
        qcfg = self._quality_config()
        sensitive_keywords: List[str] = qcfg.get(
            "sensitive_keywords",
            [
                "average",
                "mean",
                "median",
                "sum",
                "total",
                "trend",
                "correlation",
                "regression",
                "distribution",
                "forecast",
            ],
        )
        missing_threshold: float = float(qcfg.get("missing_ratio_threshold", 0.10))
        duplicate_threshold: float = float(qcfg.get("duplicate_ratio_threshold", 0.001))

        question_lower = question.lower()
        is_sensitive = any(k in question_lower for k in sensitive_keywords)
        has_quality_issues = (
            quality.get("missing_ratio", 0) > missing_threshold
            or quality.get("duplicate_ratio", 0) > duplicate_threshold
            or len(quality.get("dtype_mismatch_columns", [])) > 0
            or len(quality.get("outlier_columns", [])) > 0
        )
        return is_sensitive and has_quality_issues

    async def _llm_cleaning_judgement(
        self, quality: Dict[str, Any], question: str
    ) -> Tuple[bool, str]:
        raw = ""
        messages = [
            {
                "role": "system",
                "content": (
                    "You evaluate if cleaning should run before analysis. "
                    "Return STRICT JSON with keys: needs_cleaning (bool), reason (str)."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n"
                    f"Quality summary: {json.dumps(quality, default=str)}\n"
                    "Respond only JSON."
                ),
            },
        ]
        try:
            self._log("llm_cleaning_judgement:start", session_id=None)
            raw = await self.generate_response(messages, temperature=0.0)
            parsed = self._extract_json(raw)
            needs_cleaning = bool(parsed.get("needs_cleaning", False))
            reason = str(parsed.get("reason", ""))
            self._log(
                "llm_cleaning_judgement:completed",
                session_id=None,
                needs_cleaning=needs_cleaning,
                reason_preview=reason[:120],
            )
            return needs_cleaning, reason
        except Exception as e:
            self._log(
                "llm_cleaning_judgement:failed",
                session_id=None,
                error=str(e),
            )
            logger.warning(
                "[AnalysisAgent:llm_cleaning_judgement:raw_response] %s",
                (raw[:2000] if raw else "<empty>"),
            )
            return False, ""

    def _extract_json(self, text: str) -> Dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())

    def _build_cleaning_consent_prompt(
        self,
        quality: Dict[str, Any],
        prompt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        missing_pct = round(float(quality.get("missing_ratio", 0)) * 100, 2)
        duplicate_pct = round(float(quality.get("duplicate_ratio", 0)) * 100, 2)
        mismatch_count = len(quality.get("dtype_mismatch_columns", []))
        outlier_count = len(quality.get("outlier_columns", []))
        pid = prompt_id or str(uuid.uuid4())
        question = (
            "I detected data quality issues that can affect this analysis "
            f"(missing: {missing_pct}%, duplicates: {duplicate_pct}%, "
            f"dtype mismatches: {mismatch_count}, outlier-heavy columns: {outlier_count}). "
            "Do you want me to clean the working dataset before continuing?"
        )
        return {
            "prompt_id": pid,
            "kind": ACTION_KIND_CLEANING_CONSENT,
            "question": question,
            "options": [
                {"label": "Yes, clean first", "value": "accept"},
                {"label": "No, continue as-is", "value": "decline"},
            ],
            "allow_open_text": True,
            "context": quality,
        }

    def _parse_cleaning_decision(
        self,
        message: str,
        user_action: Optional[Dict[str, Any]],
        expected_prompt_id: Optional[str],
    ) -> Optional[bool]:
        """
        Synchronous fast-path: unambiguous structured action or clear text.
        Returns None when ambiguous — callers should then use _llm_parse_cleaning_decision.
        """
        if user_action:
            kind = user_action.get("kind")
            prompt_id = user_action.get("prompt_id")
            value = str(user_action.get("value", "")).strip().lower()
            if kind == ACTION_KIND_CLEANING_CONSENT and (
                not expected_prompt_id or prompt_id == expected_prompt_id
            ):
                if value in {"accept", "yes", "y"}:
                    return True
                if value in {"decline", "no", "n"}:
                    return False
            return None

        normalized = message.strip().lower()
        # Unambiguous exact matches only — no broad regex
        yes_values = {
            "yes",
            "y",
            "clean",
            "proceed with cleaning",
            "go ahead",
            "sure",
            "ok",
            "okay",
            "yeah",
            "yep",
            "please do",
            "do it",
        }
        no_values = {
            "no",
            "n",
            "skip cleaning",
            "continue as-is",
            "dont",
            "don't",
            "nope",
            "nah",
            "not now",
            "skip",
            "no thanks",
        }
        if normalized in yes_values:
            return True
        if normalized in no_values:
            return False
        # Single strong signal words — only when the message is short (<6 words)
        words = normalized.split()
        if len(words) <= 6:
            if re.search(r"\byes\b", normalized) and not re.search(r"\bno\b", normalized):
                return True
            if re.search(r"\bno\b", normalized) and not re.search(r"\byes\b", normalized):
                return False
        return None  # Ambiguous — caller should use _llm_parse_cleaning_decision

    async def _llm_parse_cleaning_decision(self, message: str) -> Optional[bool]:
        """
        LLM fallback for ambiguous cleaning consent replies.
        Sends a one-word prompt and interprets YES/NO from the raw response.
        """
        qcfg = self._quality_config()
        prompt_template = qcfg.get("cleaning_consent_prompt", "")
        if not prompt_template:
            prompt_template = (
                'A user was asked: "Do you want me to clean the data before analysis?"\n'
                'Their reply was: "{message}"\n'
                "Answer with exactly one word — YES or NO — based on their intent.\n"
                "If ambiguous, answer NO."
            )
        prompt = prompt_template.format(message=message)
        from llm.models import Message as LLMMessage

        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content="Answer with exactly one word: YES or NO."),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=5,
        )
        check_usage_limit()
        try:
            response = await self.llm_router.generate(request)
            word = (response.content or "").strip().upper()
            if "YES" in word:
                return True
            if "NO" in word:
                return False
        except Exception as exc:
            logger.warning("[AnalysisAgent] _llm_parse_cleaning_decision failed: %s", exc)
        return None

    async def _answer_without_execution(
        self,
        dataframe: pd.DataFrame,
        question: str,
        stats: Dict[str, Any],
    ) -> str:
        self._log(
            "answer_without_execution:start",
            session_id=None,
            question_preview=question[:120],
        )
        # Build compact column info so the LLM can reason about the data
        col_info = ", ".join(
            f"{col} ({dtype})" for col, dtype in dataframe.dtypes.astype(str).items()
        )
        shape = dataframe.shape
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a data analysis expert. "
                    "Answer the user's question directly with a clear, concise narrative. "
                    "Do NOT write any code. Do NOT include code blocks of any kind. "
                    "Provide only insights, numbers, and explanations in plain text or markdown."
                ),
            },
            {
                "role": "system",
                "content": (
                    f"Dataset: {shape[0]} rows × {shape[1]} columns\n"
                    f"Columns: {col_info}\n"
                    f"Key stats: {stats}"
                ),
            },
            {"role": "user", "content": question},
        ]
        result = await self.generate_response(messages)
        # Strip any code fences the LLM snuck in anyway
        import re as _re

        result = _re.sub(r"```[\w]*\n[\s\S]*?```", "", result).strip()
        self._log("answer_without_execution:completed", session_id=None)
        return result

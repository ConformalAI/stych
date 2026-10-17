from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

from agents.sql_agent.cleaning import SQLCleaningService
from agents.sql_agent.runner import _sanitize_identifier
from datasources.sql_readonly import require_attached_asset_placeholder
from tools.sql_copies import (
    WORKING_COPY_TOOL_NAMES,
    WORKING_COPY_TOOLS,
    execute_working_copy_tool,
)
from tools.sql_write import SQL_WRITE_TEMP_TOOL, execute_sql_write_temp
from tools.workspace_toolset import WORKSPACE_FILE_TOOLS, WorkspaceToolset
from workspace.manager import WorkspaceManager
from workspace.runner import WorkspaceSandboxRunner

logger = logging.getLogger(__name__)

_PREVIEW_ROWS = 20
_MAX_OBS_CHARS = 6000
_TOOL_INPUT_STRING_TRUNCATE_AT = 512
_TOOL_INPUT_STRING_TRUNCATE_TO = 128
_TOOL_INPUT_MAX_JSON_CHARS = 4 * 1024
_TOOL_INPUT_MAX_COLLECTION_ITEMS = 20
_TOOL_INPUT_MAX_DEPTH = 2
_SQL_PREVIEW_MAX_COLS = 50
_MONGO_SCHEMA_SAMPLE_DOCUMENTS = 1_000


def _truncate(text: str, max_chars: int = _MAX_OBS_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… [truncated, {len(text) - max_chars} chars omitted]"


def _truncate_tool_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) > _TOOL_INPUT_STRING_TRUNCATE_AT:
            return f"{value[:_TOOL_INPUT_STRING_TRUNCATE_TO]}…[{len(value)} chars]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= _TOOL_INPUT_MAX_DEPTH:
        return "<nested>"
    if isinstance(value, list):
        mapped = [
            _truncate_tool_value(v, depth + 1) for v in value[:_TOOL_INPUT_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _TOOL_INPUT_MAX_COLLECTION_ITEMS:
            mapped.append(f"…[{len(value)} items]")
        return mapped
    if isinstance(value, dict):
        items = list(value.items())
        mapped = [
            (k, _truncate_tool_value(v, depth + 1))
            for k, v in items[:_TOOL_INPUT_MAX_COLLECTION_ITEMS]
        ]
        if len(items) > _TOOL_INPUT_MAX_COLLECTION_ITEMS:
            mapped.append(("…", f"{len(items)} keys"))
        return dict(mapped)
    return str(value)


def _truncate_tool_json(value: Any) -> str:
    truncated = _truncate_tool_value(value)
    try:
        payload = json.dumps(truncated, default=str)
    except Exception:
        payload = str(truncated)
    if len(payload) > _TOOL_INPUT_MAX_JSON_CHARS:
        payload = payload[:_TOOL_INPUT_MAX_JSON_CHARS] + "…[truncated]"
    return payload


def _json_dumps_safe(value: Any, *, max_chars: int = _MAX_OBS_CHARS) -> str:
    """
    Serialize JSON for LLM consumption without depth-collapsing dicts into "<nested>".

    The generic _truncate_tool_json() is depth-based and will replace row dicts with
    "<nested>" (payload->data(list)->row(dict)), which makes SQL previews unusable.
    Here we rely on:
    - limiting row count upstream (_PREVIEW_ROWS)
    - stringifying non-JSON types via default=str
    - truncating by character count only
    """
    try:
        s = json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return _truncate(s, max_chars=max_chars)


def _extract_sql_arg(tool_input: Union[str, Dict[str, Any]], *keys: str) -> str:
    """
    Pull a SQL string out of *tool_input*, accepting any of *keys* as the arg name.

    The model occasionally varies the parameter name (sql / query / select_sql). Accepting
    aliases here keeps the advertised schema name and the handler decoupled, so a future
    rename of one cannot silently break execution (the bug class this audit fixed). A bare
    string input is treated as the SQL itself.
    """
    if isinstance(tool_input, str):
        return tool_input.strip()
    if isinstance(tool_input, dict):
        for k in keys:
            v = tool_input.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


# Connector-specific primitives. Working-copy lifecycle tools are deliberately
# excluded: they are shared by SQL and MongoDB sessions and must pass either
# connector's availability guard.
_SQL_NATIVE_TOOLS = frozenset(
    {
        "sql_read",
        "sql_preview",
        "sql_materialize_copy",
        "sql_write_temp",
        "sql_write_copy",
        "create_copy_table",
        "create_view",
    }
)
SQL_TOOLS: frozenset = _SQL_NATIVE_TOOLS | WORKING_COPY_TOOL_NAMES

# Native tool schemas for the SQL tools, co-located with the handlers below so the
# advertised parameter names cannot drift from what the handlers read (the drift that
# made sql_read silently ignore the model's query and return the whole table). The
# cleaning agent imports this list instead of maintaining a parallel copy.
SQL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "sql_read",
            "description": (
                "Run a read-only SELECT (or WITH/CTE) against the PostgreSQL temp table and "
                "return the result rows. ALWAYS reference the table with the {table} placeholder. "
                'Example: {"sql": "SELECT status, COUNT(*) FROM {table} GROUP BY status"}.'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A single SELECT/WITH statement using the {table} placeholder.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["temp", "copy"],
                        "description": "Which table {table} resolves to (default: temp).",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql_preview",
            "description": (
                "Preview rows from the temp table as explicit columns + row dicts. Same rules as "
                "sql_read: a single SELECT/WITH referencing the table via {table}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A single SELECT/WITH statement using the {table} placeholder.",
                    },
                    "target": {"type": "string", "enum": ["temp", "copy"]},
                },
                "required": ["sql"],
            },
        },
    },
    SQL_WRITE_TEMP_TOOL,
    {
        "type": "function",
        "function": {
            "name": "sql_materialize_copy",
            "description": (
                "Create or replace a PERMANENT table in the user's database schema in ONE "
                "approved step. Use only when the user explicitly requested database "
                "persistence; never use for a private Stych working copy, whose scratch edits "
                "are already durable for preview/diff/analysis. This operation "
                "creates the copy table if needed, then replaces its contents (TRUNCATE + "
                "INSERT). Requires user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "select_sql": {
                        "type": "string",
                        "description": (
                            "Optional. Defaults to 'SELECT * FROM {source_table}', which persists "
                            "the whole cleaned working table. Provide only to transform inline; "
                            "use the {source_table} placeholder."
                        ),
                    },
                    "source_target": {"type": "string", "enum": ["temp", "source"]},
                    "target_table": {
                        "type": "string",
                        "description": (
                            "Optional destination table name in the user's schema (default: "
                            "copy_<table>). Use a distinct name when the user wants a separate "
                            "copy or to avoid overwriting an existing one."
                        ),
                    },
                    "approved": {"type": "boolean"},
                },
                "required": ["approved"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql_write_copy",
            "description": (
                "Apply an in-place data-modifying statement (UPDATE/DELETE/ALTER) to the copy "
                "table. To replace the copy table contents from a cleaned query, use "
                "sql_materialize_copy instead. Requires user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "In-place UPDATE/DELETE/ALTER using {table} (not SELECT).",
                    },
                    "approved": {"type": "boolean"},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_copy_table",
            "description": (
                "Create the persistent copy table (a duplicate of the source) so cleaned data can be "
                "written without touching the original. Requires user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_view",
            "description": (
                "Create or replace a public view pointing at the cleaned table. Requires user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "view_name": {"type": "string"},
                    "target": {"type": "string", "enum": ["temp", "copy"]},
                    "approved": {"type": "boolean"},
                },
                "required": ["view_name"],
            },
        },
    },
    *WORKING_COPY_TOOLS,
]

MONGO_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "mongo_read",
            "description": (
                "Run a read-only aggregation pipeline against the active MongoDB working copy. "
                "$lookup/$unionWith and server-side JavaScript are refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline": {
                        "type": "string",
                        "description": "JSON array of aggregation stages.",
                    }
                },
                "required": ["pipeline"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mongo_update",
            "description": (
                "Update documents in the private working copy. filter and update are JSON; "
                "update may be an operator document or aggregation update pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "JSON match document."},
                    "update": {"type": "string", "description": "JSON update document or array."},
                },
                "required": ["filter", "update"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mongo_delete",
            "description": "Delete matching documents from the private working copy.",
            "parameters": {
                "type": "object",
                "properties": {"filter": {"type": "string", "description": "JSON match document."}},
                "required": ["filter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mongo_deduplicate",
            "description": "Remove duplicate documents, keeping the lowest _id for each field tuple.",
            "parameters": {
                "type": "object",
                "properties": {"fields": {"type": "array", "items": {"type": "string"}}},
                "required": ["fields"],
            },
        },
    },
] + WORKING_COPY_TOOLS

_MONGO_NATIVE_TOOLS = frozenset({"mongo_read", "mongo_update", "mongo_delete", "mongo_deduplicate"})
MONGO_TOOLS = _MONGO_NATIVE_TOOLS | WORKING_COPY_TOOL_NAMES

# Tools always available regardless of datasource.
_BASE_TOOLS: frozenset = WORKSPACE_FILE_TOOLS


class CleaningReactToolRegistry:
    """
    Low-level tool registry for the ReAct cleaning agent.
    These tools are intentionally primitive and sandboxed.

    SQL tools are only included when ``is_sql_available=True`` (i.e. the session
    has an active PostgreSQL connection).  Calling a SQL tool without a connection
    returns a clear error string before any execution occurs.
    """

    # Full set kept for backward-compatible imports; runtime gating uses available_tools.
    TOOL_NAMES: frozenset = _BASE_TOOLS | SQL_TOOLS | MONGO_TOOLS

    def __init__(
        self,
        sql_agent: Any,
        workspace_manager: WorkspaceManager,
        workspace_runner: WorkspaceSandboxRunner,
        is_sql_available: bool = False,
        is_mongo_available: bool = False,
    ) -> None:
        self._sql_agent = sql_agent
        self._workspace_manager = workspace_manager
        self._workspace_runner = workspace_runner
        self._toolset = WorkspaceToolset(workspace_runner)
        self._sql_cleaning_service = SQLCleaningService(sql_agent)
        self.is_sql_available = is_sql_available
        self.is_mongo_available = is_mongo_available

    @property
    def available_tools(self) -> frozenset:
        """Return the set of tools usable in this session."""
        if self.is_mongo_available:
            return MONGO_TOOLS
        return _BASE_TOOLS | SQL_TOOLS if self.is_sql_available else _BASE_TOOLS

    async def call(
        self,
        tool_name: str,
        tool_input: Union[str, Dict[str, Any]],
        state: Optional[Any] = None,
    ) -> str:
        # Layer 3+4: hard guard — reject SQL tools when no connection is active.
        if tool_name in _SQL_NATIVE_TOOLS and not self.is_sql_available:
            return (
                "ERROR: SQL tools are not available — no PostgreSQL connection is active for "
                "this session. Use file/workspace tools (list_files, read_file, write_file, "
                "run_script) to work with the uploaded CSV instead."
            )
        if tool_name in _MONGO_NATIVE_TOOLS and not self.is_mongo_available:
            return "ERROR: MongoDB cleaning tools are not available for this session."
        if tool_name not in self.available_tools:
            return (
                f"ERROR: Unknown tool '{tool_name}'. "
                f"Available tools: {sorted(self.available_tools)}"
            )

        try:
            if tool_name in WORKSPACE_FILE_TOOLS:
                # run_script spawns a blocking subprocess; offload to a worker thread
                # so the event loop keeps draining the SSE queue and the live trace
                # streams instead of freezing until the script finishes.
                return await asyncio.to_thread(
                    self._call_workspace_tool, tool_name, tool_input, state
                )
            if tool_name == "sql_read":
                return await self._sql_read(tool_input, state)
            if tool_name == "sql_preview":
                return await self._sql_preview(tool_input, state)
            if tool_name == "sql_materialize_copy":
                return await self._sql_materialize_copy(tool_input, state)
            if tool_name == "sql_write_temp":
                return await self._sql_write_temp(tool_input, state)
            if tool_name == "sql_write_copy":
                return await self._sql_write_copy(tool_input, state)
            if tool_name == "mongo_read":
                return await self._mongo_read(tool_input, state)
            if tool_name == "mongo_update":
                return await self._mongo_update(tool_input, state)
            if tool_name == "mongo_delete":
                return await self._mongo_delete(tool_input, state)
            if tool_name == "mongo_deduplicate":
                return await self._mongo_deduplicate(tool_input, state)
            if tool_name == "create_copy_table":
                return await self._create_copy_table(tool_input, state)
            if tool_name == "create_view":
                return await self._create_view(tool_input, state)
            if tool_name in WORKING_COPY_TOOL_NAMES:
                return await execute_working_copy_tool(
                    self._sql_agent, state, tool_name, tool_input
                )
        except Exception as exc:
            logger.warning("[CleaningReactToolRegistry] tool=%s error=%s", tool_name, exc)
            return f"ERROR calling {tool_name}: {str(exc)[:2000]}"

        return "ERROR: Unhandled tool dispatch."

    @staticmethod
    def _mongo_json(tool_input: Union[str, Dict[str, Any]], key: str, default: Any) -> Any:
        raw = tool_input if isinstance(tool_input, str) else (tool_input or {}).get(key, default)
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(str(raw or json.dumps(default)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key} must be valid JSON: {exc}") from exc

    async def _mongo_target(self, state: Optional[Any]):
        if not self._sql_agent or not state or not state.data_context:
            raise ValueError("MongoDB datasource is not available")
        database, collection = await self._sql_agent.ensure_temp_table(state)
        return self._sql_agent._runner_for(state), database, collection

    async def _refresh_mongo_sample(
        self, runner: Any, database: str, collection: str, state: Optional[Any]
    ) -> None:
        """Refresh sampled fields after a document mutation.

        MongoDB schemas are inferred. Keeping the connect-time frame after a
        rename/add/remove would hide the new working-copy fields from preview
        and visualization, so mutations refresh the documented 1,000-document
        schema sample without materializing the full collection.
        """
        if state is None or state.data_context is None:
            return
        docs = await asyncio.to_thread(
            runner.run_pipeline,
            collection,
            [{"$limit": _MONGO_SCHEMA_SAMPLE_DOCUMENTS}],
            database=database,
            allow_other_collections=False,
            allow_writes=False,
        )
        if docs:
            from datasources.mongodb import flatten_documents  # noqa: PLC0415

            state.data_context.working_dataframe = flatten_documents(docs)
        state.data_context.metadata["schema_sample_documents"] = _MONGO_SCHEMA_SAMPLE_DOCUMENTS

    async def _mongo_read(self, tool_input: Any, state: Optional[Any]) -> str:
        stages = self._mongo_json(tool_input, "pipeline", [])
        if not isinstance(stages, list):
            return "ERROR: pipeline must be a JSON array."
        runner, database, collection = await self._mongo_target(state)
        docs = await asyncio.to_thread(
            runner.run_pipeline,
            collection,
            stages + [{"$limit": _PREVIEW_ROWS + 1}],
            database=database,
            allow_other_collections=False,
            allow_writes=False,
        )
        from datasources.mongodb import flatten_documents  # noqa: PLC0415

        frame = flatten_documents(docs[:_PREVIEW_ROWS])
        return _json_dumps_safe(
            {
                "documents": len(docs),
                "preview": frame.head(_PREVIEW_ROWS).to_dict(orient="records"),
                "truncated": len(docs) > _PREVIEW_ROWS,
            }
        )

    async def _mongo_update(self, tool_input: Any, state: Optional[Any]) -> str:
        filter_doc = self._mongo_json(tool_input, "filter", {})
        update = self._mongo_json(tool_input, "update", {})
        if not isinstance(filter_doc, dict):
            return "ERROR: filter must be a JSON object."
        runner, database, collection = await self._mongo_target(state)
        changed = await asyncio.to_thread(runner.update_many, collection, filter_doc, update)
        await self._refresh_mongo_sample(runner, database, collection, state)
        SQLCleaningService._bump_temp_generation(state)
        return f"Executed MongoDB update on the working copy; modified {changed} document(s)."

    async def _mongo_delete(self, tool_input: Any, state: Optional[Any]) -> str:
        filter_doc = self._mongo_json(tool_input, "filter", {})
        if not isinstance(filter_doc, dict):
            return "ERROR: filter must be a JSON object."
        runner, database, collection = await self._mongo_target(state)
        changed = await asyncio.to_thread(runner.delete_many, collection, filter_doc)
        await self._refresh_mongo_sample(runner, database, collection, state)
        SQLCleaningService._bump_temp_generation(state)
        return f"Executed MongoDB delete on the working copy; removed {changed} document(s)."

    async def _mongo_deduplicate(self, tool_input: Any, state: Optional[Any]) -> str:
        fields = tool_input.get("fields", []) if isinstance(tool_input, dict) else []
        if not isinstance(fields, list) or not fields:
            return "ERROR: fields must be a non-empty array."
        runner, database, collection = await self._mongo_target(state)
        changed = await asyncio.to_thread(runner.deduplicate, collection, fields)
        await self._refresh_mongo_sample(runner, database, collection, state)
        SQLCleaningService._bump_temp_generation(state)
        return f"Executed MongoDB deduplication; removed {changed} duplicate document(s)."

    # ------------------------------------------------------------------
    # Workspace tools
    # ------------------------------------------------------------------

    def _workspace_root(self, state: Optional[Any]) -> Path:
        if not state:
            raise ValueError("state is required for workspace tools")
        user_id = state.metadata.get("user_id") if state.metadata else None
        return self._workspace_manager.get_or_create(state.session_id, user_id=user_id)

    def _call_workspace_tool(
        self,
        tool_name: str,
        tool_input: Union[str, Dict[str, Any]],
        state: Optional[Any],
    ) -> str:
        """Delegate file/script tools to the shared, sandboxed WorkspaceToolset.

        ``run_script`` reloads the active dataset into working_dataframe when a script
        changes it, and refreshes the artifact manifest (see WorkspaceToolset._sync_after_run).
        """
        root = self._workspace_root(state)
        ts = self._toolset
        if tool_name == "list_files":
            return ts.list_files(root)
        if tool_name == "read_file":
            return ts.read_file(root, tool_input)
        if tool_name == "write_file":
            return ts.write_file(root, tool_input)
        if tool_name == "edit_file":
            return ts.edit_file(root, tool_input)
        if tool_name == "delete_file":
            return ts.delete_file(root, tool_input)
        if tool_name == "run_script":
            return ts.run_script(root, tool_input, state)
        if tool_name == "set_active_dataset":
            return ts.set_active_dataset(root, tool_input, state)
        return f"ERROR: Unknown workspace tool '{tool_name}'."

    # ------------------------------------------------------------------
    # SQL tools
    # ------------------------------------------------------------------

    async def _sql_read(self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]) -> str:
        if not self._sql_agent or not state or not state.data_context:
            return "ERROR: SQL datasource is not available."
        sql = _extract_sql_arg(tool_input, "sql", "query")
        target = tool_input.get("target", "temp") if isinstance(tool_input, dict) else "temp"
        # No silent default: an empty/misnamed arg must surface, not run the whole table.
        if not sql:
            return (
                'ERROR: No SQL provided. Pass {"sql": "SELECT ... FROM {table} ..."} — '
                "the {table} placeholder resolves to the active table."
            )
        try:
            require_attached_asset_placeholder(sql)
        except ValueError as exc:
            return f"ERROR: {exc}"

        quoted_table = await self._resolve_sql_target(state, target)
        # Only claim a resolved target when {table} was actually substituted. A literal table
        # name (no placeholder) runs as-is, so reporting the temp table would misdescribe what ran.
        substituted = "{table}" in sql
        if substituted:
            sql = sql.replace("{table}", quoted_table)

        try:
            rows = await self._sql_agent.run_sql(state, sql)
            preview = rows[:_PREVIEW_ROWS]
            payload = {
                "rows": len(rows),
                "preview_rows": len(preview),
                "data": preview,
                "truncated": len(rows) > len(preview),
                "resolved_target": target if substituted else "raw",
                "resolved_table": quoted_table if substituted else None,
            }
            logger.info(
                "[CleaningReActTool] sql_read input=%s rows=%s",
                _truncate_tool_json(tool_input),
                len(rows),
            )
            # IMPORTANT: return an actually readable preview (avoid "<nested>" row dicts).
            return _json_dumps_safe(payload)
        except Exception as exc:
            return f"SQL ERROR: {str(exc)[:2000]}"

    async def _sql_preview(
        self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]
    ) -> str:
        """
        LLM-friendly preview: returns explicit columns + rows (as list of dicts).

        Same security constraints as sql_read: SQLAgent enforces SELECT-only and the
        caller should use {table} for the allowed target.
        """
        if not self._sql_agent or not state or not state.data_context:
            return "ERROR: SQL datasource is not available."
        sql = _extract_sql_arg(tool_input, "sql", "query")
        target = tool_input.get("target", "temp") if isinstance(tool_input, dict) else "temp"
        # No silent default: an empty/misnamed arg must surface, not run the whole table.
        if not sql:
            return (
                'ERROR: No SQL provided. Pass {"sql": "SELECT ... FROM {table} ..."} — '
                "the {table} placeholder resolves to the active table."
            )
        try:
            require_attached_asset_placeholder(sql)
        except ValueError as exc:
            return f"ERROR: {exc}"

        quoted_table = await self._resolve_sql_target(state, target)
        # Only claim a resolved target when {table} was actually substituted (see _sql_read).
        substituted = "{table}" in sql
        if substituted:
            sql = sql.replace("{table}", quoted_table)

        try:
            rows = await self._sql_agent.run_sql(state, sql)
            preview = rows[:_PREVIEW_ROWS]
            # Establish a stable column order from the first row (bounded).
            columns: list[str] = []
            if preview and isinstance(preview[0], dict):
                columns = list(preview[0].keys())[:_SQL_PREVIEW_MAX_COLS]
            payload = {
                "rows": len(rows),
                "preview_rows": len(preview),
                "columns": columns,
                "rows_data": preview,
                "truncated": len(rows) > len(preview),
                # Prevent the model from guessing table names like "data".
                "resolved_target": target if substituted else "raw",
                "resolved_table": quoted_table if substituted else None,
            }
            logger.info(
                "[CleaningReActTool] sql_preview input=%s rows=%s",
                _truncate_tool_json(tool_input),
                len(rows),
            )
            return _json_dumps_safe(payload)
        except Exception as exc:
            return f"SQL ERROR: {str(exc)[:2000]}"

    async def _sql_write_temp(
        self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]
    ) -> str:
        sql = _extract_sql_arg(tool_input, "sql", "query")
        logger.info(
            "[CleaningReActTool] sql_write_temp input=%s",
            _truncate_tool_json(tool_input),
        )
        # Shared write path (also used by the analysis agent): validates, executes,
        # and bumps temp_table_generation so preview/diff caches refresh.
        return await execute_sql_write_temp(self._sql_agent, state, sql)

    async def _sql_write_copy(
        self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]
    ) -> str:
        if not self._sql_agent or not state or not state.data_context:
            return "ERROR: SQL datasource is not available."
        if isinstance(tool_input, str):
            return "ERROR: sql_write_copy expects {sql, approved}."
        sql = _extract_sql_arg(tool_input, "sql", "query")
        approved = bool(tool_input.get("approved"))
        if not approved:
            return "PERMISSION_REQUIRED: sql_write_copy requires explicit approval."
        if not sql:
            return "ERROR: No SQL provided to sql_write_copy."

        quoted_table = await self._resolve_sql_target(state, "copy")
        if not quoted_table:
            return "ERROR: Copy table not found."

        ctx = state.data_context
        schema = ctx.metadata.get("schema", "public")
        source = ctx.metadata.get("table")
        if not source:
            return "ERROR: Source table not found in session context."
        copy_table = source if source.startswith("copy_") else f"copy_{source}"
        statements = self._sql_cleaning_service.split_sql_statements(sql)
        exec_statements = [s.replace("{table}", quoted_table) for s in statements]

        connection_url = state.data_context.metadata["connection_url"]
        runner = self._sql_agent._runner(connection_url)
        user_schema = str(ctx.metadata.get("schema", "public"))
        try:
            if not runner.table_exists(schema, copy_table):
                return "ERROR: Copy table does not exist yet. Call create_copy_table (with approval) first."

            # sql_write_copy is for in-place edits only. SELECT-based replacement belongs to
            # sql_materialize_copy, which keeps source vs destination tables unambiguous.
            if exec_statements and any(
                s.strip().upper().startswith(("SELECT", "WITH")) for s in exec_statements
            ):
                return (
                    "ERROR: sql_write_copy only accepts in-place statements (UPDATE/DELETE/ALTER) "
                    "on the copy table. To replace the copy table contents from a cleaned query, "
                    "use sql_materialize_copy instead."
                )

            from agents.sql_agent.write_guard import (  # noqa: PLC0415
                WriteGuardError,
                validate_write_statements,
            )

            fold = getattr(runner, "write_fold", "lower")
            try:
                validate_write_statements(
                    exec_statements,
                    allowed_schema=user_schema,
                    allowed_table=copy_table,
                    fold=fold,
                )
            except WriteGuardError as exc:
                return f"ERROR: {exc}"

            runner.execute_statements_transaction(exec_statements, allowed_schema=user_schema)
            logger.info(
                "[CleaningReActTool] sql_write_copy input=%s statements=%s",
                _truncate_tool_json(tool_input),
                len(exec_statements),
            )
            return f"Executed {len(exec_statements)} statement(s) on copy table."
        except Exception as exc:
            return f"SQL ERROR: {str(exc)[:2000]}"

    async def _sql_materialize_copy(
        self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]
    ) -> str:
        """
        Persist the cleaned data into the copy table in ONE approved step.

        This is the single canonical write-back path for a SQL datasource:
          1. apply each cleaning step to the working (temp) table with sql_write_temp
          2. call sql_materialize_copy{approved:true}

        It creates the copy table if it does not exist yet, then atomically replaces its
        contents (TRUNCATE + INSERT) — so one approval does the whole write-back (no
        separate create_copy_table, no second permission prompt).

        ``select_sql`` is OPTIONAL: when omitted it persists the entire cleaned working
        table (``SELECT * FROM {source_table}``), which captures every accumulated
        sql_write_temp change. Supply select_sql (with the {source_table} placeholder)
        only to transform further inline.

        Input (dict):
          - select_sql: optional; defaults to "SELECT * FROM {source_table}".
          - source_target: "temp" (default) | "source"
          - approved: required true
        """
        if not self._sql_agent or not state or not state.data_context:
            return "ERROR: SQL datasource is not available."
        if isinstance(tool_input, str):
            return "ERROR: sql_materialize_copy expects {select_sql?, source_target?, approved}."

        approved = bool(tool_input.get("approved"))
        if not approved:
            return "PERMISSION_REQUIRED: sql_materialize_copy requires explicit approval."

        ctx = state.data_context
        source = ctx.metadata.get("table")
        if not source:
            return "ERROR: Cannot materialize a copy for custom SQL query sources."

        # select_sql is optional: default to persisting the full cleaned table so the
        # reliable path (clean temp -> materialize) needs no hand-written SQL.
        select_sql = _extract_sql_arg(tool_input, "select_sql", "sql", "query")
        if not select_sql:
            select_sql = "SELECT * FROM {source_table}"
        if not select_sql.upper().startswith(("SELECT", "WITH")):
            return "ERROR: select_sql must start with SELECT or WITH."

        source_target = str(tool_input.get("source_target") or "temp").strip().lower()
        if source_target not in {"temp", "source"}:
            return "ERROR: source_target must be 'temp' or 'source'."

        schema = ctx.metadata.get("schema", "public")
        custom_target = str(tool_input.get("target_table") or "").strip()
        if custom_target:
            copy_table = _sanitize_identifier(custom_target)
            if copy_table == source:
                return "ERROR: target_table must differ from the source table."
        else:
            copy_table = source if source.startswith("copy_") else f"copy_{source}"
        dest_table = f'"{schema}"."{copy_table}"'

        if source_target == "temp":
            source_table = await self._resolve_sql_target(state, "temp")
        else:
            source_table = f'"{schema}"."{source}"'

        # Rewrite placeholders ({source_table}, and {table} for back-compat).
        rewritten = select_sql.replace("{source_table}", source_table).replace(
            "{table}", source_table
        )

        connection_url = ctx.metadata["connection_url"]
        runner = self._sql_agent._runner(connection_url)

        try:
            # Create the copy table on first write-back so a single approval persists
            # everything — avoids the failure mode where the model cleans the temp table
            # and then claims persistence without ever creating/populating the copy.
            if not runner.table_exists(schema, copy_table):
                if custom_target:
                    # A user-named copy is created straight from the SELECT so schema
                    # changes made on the working copy (added columns, recasts) carry over.
                    runner.execute_statements_transaction(
                        [f"CREATE TABLE {dest_table} AS {rewritten.rstrip(';')}"],
                        allowed_schema=schema,
                    )
                    rows_written = runner.get_row_count(schema, copy_table)
                    logger.info(
                        "[CleaningReActTool] sql_materialize_copy created %s rows=%s",
                        dest_table,
                        rows_written,
                    )
                    return _json_dumps_safe(
                        {
                            "status": "ok",
                            "source_target": source_target,
                            "source_table": source_table,
                            "dest_table": dest_table,
                            "rows_written": rows_written,
                        }
                    )
                runner._execute(
                    f'CREATE TABLE "{schema}"."{copy_table}" '
                    f'(LIKE "{schema}"."{source}" INCLUDING ALL)'
                )
            truncate_sql = f"TRUNCATE TABLE {dest_table}"
            insert_sql = f"INSERT INTO {dest_table} {rewritten.rstrip(';')}"
            runner.execute_statements_transaction([truncate_sql, insert_sql], allowed_schema=schema)
            rows_written = runner.get_row_count(schema, copy_table)
            logger.info(
                "[CleaningReActTool] sql_materialize_copy source_target=%s rows_written=%s",
                source_target,
                rows_written,
            )
            return _json_dumps_safe(
                {
                    "status": "ok",
                    "source_target": source_target,
                    "source_table": source_table,
                    "dest_table": dest_table,
                    "rows_written": rows_written,
                }
            )
        except Exception as exc:
            return f"SQL ERROR: {str(exc)[:2000]}"

    async def _create_copy_table(
        self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]
    ) -> str:
        if not self._sql_agent or not state or not state.data_context:
            return "ERROR: SQL datasource is not available."
        if isinstance(tool_input, str):
            return "ERROR: create_copy_table expects {approved}."
        approved = bool(tool_input.get("approved"))
        if not approved:
            return "PERMISSION_REQUIRED: create_copy_table requires explicit approval."
        ctx = state.data_context
        if not ctx.metadata.get("table"):
            return "ERROR: Cannot create copy table for custom SQL sources."

        connection_url = ctx.metadata["connection_url"]
        runner = self._sql_agent._runner(connection_url)
        schema = ctx.metadata.get("schema", "public")
        source = ctx.metadata.get("table")
        copy_table = source if source.startswith("copy_") else f"copy_{source}"

        try:
            if not runner.table_exists(schema, copy_table):
                create_sql = (
                    f'CREATE TABLE "{schema}"."{copy_table}" '
                    f'(LIKE "{schema}"."{source}" INCLUDING ALL)'
                )
                insert_sql = (
                    f'INSERT INTO "{schema}"."{copy_table}" ' f'SELECT * FROM "{schema}"."{source}"'
                )
                runner._execute(create_sql)
                runner._execute(insert_sql)
            return f"Copy table ready: {schema}.{copy_table}"
        except Exception as exc:
            return f"SQL ERROR: {str(exc)[:2000]}"

    async def _create_view(
        self, tool_input: Union[str, Dict[str, Any]], state: Optional[Any]
    ) -> str:
        if not self._sql_agent or not state or not state.data_context:
            return "ERROR: SQL datasource is not available."
        if isinstance(tool_input, str):
            return "ERROR: create_view expects {approved, view_name, target}."
        approved = bool(tool_input.get("approved"))
        if not approved:
            return "PERMISSION_REQUIRED: create_view requires explicit approval."

        view_name = tool_input.get("view_name") or tool_input.get("name") or ""
        target = tool_input.get("target", "temp")
        if not view_name:
            return "ERROR: Missing view_name."

        schema, table = await self._resolve_sql_relation(state, target)
        if not table:
            return "ERROR: Target table not found."

        connection_url = state.data_context.metadata["connection_url"]
        runner = self._sql_agent._runner(connection_url)
        try:
            runner.create_or_replace_view("public", view_name, schema, table)
            return f"View public.{view_name} now points to {schema}.{table}."
        except Exception as exc:
            return f"SQL ERROR: {str(exc)[:2000]}"

    async def _resolve_sql_relation(self, state: Any, target: str) -> tuple[str, str]:
        """Unquoted ``(schema, table)`` for a tool target.

        Kept separate from the quoted form because callers that hand the
        relation to another API need the bare names; recovering them by
        stripping quotes off the rendered string cannot survive dialects that
        quote differently or address a catalog.
        """
        if target == "source":
            ctx = state.data_context
            return (
                str(ctx.metadata.get("schema") or "public"),
                str(ctx.metadata.get("table") or ""),
            )

        temp_schema, temp_table = await self._sql_agent.ensure_temp_table(state)
        if target == "copy":
            ctx = state.data_context
            schema = ctx.metadata.get("schema", "public")
            source = ctx.metadata.get("table")
            if not source:
                return ("", "")
            copy_table = source if source.startswith("copy_") else f"copy_{source}"
            return (schema, copy_table)
        return (temp_schema, temp_table)

    async def _resolve_sql_target(self, state: Any, target: str) -> str:
        """The ``{table}`` substitution, quoted the way this source expects.

        The runner owns quoting: Databricks needs backticks and a catalog
        prefix, T-SQL needs brackets, and only the Postgres-shaped engines
        accept double quotes. Hardcoding one spelling here made every
        agent-authored statement a parse error on the others.
        """
        schema, table = await self._resolve_sql_relation(state, target)
        if not table:
            return ""
        runner = self._sql_agent._runner_for(state)
        if target == "source" and callable(getattr(runner, "_source_ref", None)):
            # Databricks source tables can live in a different Unity Catalog
            # catalog from Stych's scratch schema.
            return runner._source_ref(schema, table)
        return runner.quote_relation(schema, table)

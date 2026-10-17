"""
ReAct tool registry for the DataAnalysisAgent.

File-based, sandboxed tools shared with the cleaning agent (``list_files``,
``read_file``, ``write_file``, ``edit_file``, ``delete_file``, ``run_script``) plus
analysis-specific helpers (``get_schema``, ``run_sql``, ``needs_cleaning``). Each tool
returns a plain-text observation string fed back into the ReAct loop.

The dataset lives in the workspace as ``data.parquet`` (canonical, typed) with a
``data.csv`` mirror. Scripts load it themselves (``pd.read_parquet('data.parquet')``)
and write it back to persist changes — there is no magic ``df``/``result`` variable.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

import pandas as pd

from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements
from tools.sql_copies import WORKING_COPY_TOOL_NAMES, execute_working_copy_tool
from tools.sql_write import execute_sql_write_temp
from tools.workspace_toolset import WORKSPACE_FILE_TOOLS, WorkspaceToolset

logger = logging.getLogger(__name__)

# Maximum rows to show in tabular observations — keeps context window manageable.
_PREVIEW_ROWS = 10
# Maximum characters for any single observation.
_MAX_OBS_CHARS = 4000


def _truncate(text: str, max_chars: int = _MAX_OBS_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… [truncated, {len(text) - max_chars} chars omitted]"


def _format_one_query_result(
    *,
    row_count: int,
    preview: pd.DataFrame,
    truncated: bool = False,
    extra: str = "",
    index: int = 1,
    total: int = 1,
) -> str:
    if row_count == 0:
        body = "Query returned no rows."
    else:
        cap = " (result capped)." if truncated else "."
        body = (
            f"Query returned {row_count} rows{cap}{extra} "
            f"First {len(preview)} shown:\n" + preview.to_markdown(index=False)
        )
    if total > 1:
        return f"--- Query {index}/{total} ---\n{body}"
    return body


def _format_sql_frames(frames: list[pd.DataFrame]) -> str:
    if not frames:
        return "Query returned no rows."
    parts = []
    total = len(frames)
    for index, frame in enumerate(frames, start=1):
        if frame is None or frame.empty:
            parts.append(
                _format_one_query_result(
                    row_count=0, preview=pd.DataFrame(), index=index, total=total
                )
            )
            continue
        preview = frame.head(_PREVIEW_ROWS)
        parts.append(
            _format_one_query_result(
                row_count=len(frame),
                preview=preview,
                index=index,
                total=total,
            )
        )
    return _truncate("\n\n".join(parts))


class ReactToolRegistry:
    """
    Callable tool registry used by _execute_react_loop inside DataAnalysisAgent.

    All tools return a *string* observation so the LLM can reason over it in
    the next iteration of the ReAct loop. File/script tools delegate to the shared
    sandboxed ``WorkspaceToolset``; ``run_script`` reloads the canonical dataset into
    working_dataframe when a script changes it.
    """

    # Tool names the LLM is allowed to call.
    TOOL_NAMES = (
        WORKSPACE_FILE_TOOLS
        | {"run_sql", "run_pipeline", "sql_write_temp", "get_schema", "needs_cleaning"}
        | WORKING_COPY_TOOL_NAMES
    )

    def __init__(
        self,
        toolset: WorkspaceToolset,
        sql_agent: Optional[Any],  # SQLAgent | None
        *,
        workspace_root: Optional[Path] = None,
        quality_summary_fn: Optional[Callable[[pd.DataFrame], Dict[str, Any]]] = None,
        sql_schema_text: Optional[str] = None,
    ) -> None:
        self._toolset = toolset
        self._sql_agent = sql_agent
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root is not None else None
        )
        self._quality_summary_fn = quality_summary_fn
        # For SQL sessions: the real Postgres table ref + column types, used by get_schema
        # (instead of the in-memory preview frame's dtypes).
        self._sql_schema_text = sql_schema_text

    async def call(
        self,
        tool_name: str,
        tool_input: Union[str, Dict[str, Any]],
        df: pd.DataFrame,
        state: Optional[Any] = None,
    ) -> str:
        """Dispatch to the named tool and return a string observation."""
        if tool_name not in self.TOOL_NAMES:
            return f"ERROR: Unknown tool '{tool_name}'. Available tools: {sorted(self.TOOL_NAMES)}"

        try:
            if tool_name in WORKSPACE_FILE_TOOLS:
                # run_script spawns a blocking subprocess; offload the whole
                # workspace-tool dispatch to a worker thread so the event loop keeps
                # draining the SSE queue (otherwise the live trace freezes until the
                # script finishes, then flushes all at once).
                return await asyncio.to_thread(
                    self._call_workspace_tool, tool_name, tool_input, state
                )

            if tool_name == "needs_cleaning":
                return self._needs_cleaning(tool_input, df, state)

            if tool_name == "run_sql":
                query = tool_input if isinstance(tool_input, str) else tool_input.get("query", "")
                return await self._run_sql(query, df, state)

            if tool_name == "run_pipeline":
                return await self._run_pipeline(tool_input, state)

            if tool_name == "sql_write_temp":
                return await self._sql_write_temp(tool_input, state)

            if tool_name in WORKING_COPY_TOOL_NAMES:
                return await execute_working_copy_tool(
                    self._sql_agent, state, tool_name, tool_input
                )

            if tool_name == "get_schema":
                if not self._sql_schema_text and state and state.data_context:
                    from execution.session_plane import (  # noqa: PLC0415
                        PlaneKind,
                        resolve_data_plane,
                    )

                    plane = await asyncio.to_thread(resolve_data_plane, state, state.data_context)
                    if plane.kind == PlaneKind.DUCKDB and plane.relation is not None:
                        lines = [
                            f'DuckDB schema for "{plane.relation_name}" '
                            f"({plane.scope.value} snapshot, {plane.row_count} rows):"
                        ]
                        lines.extend(
                            f"  {column.name} ({column.data_type})"
                            for column in plane.relation.columns
                        )
                        return "\n".join(lines)
                return self._get_schema(df)

        except Exception as exc:
            logger.warning("[ReactToolRegistry] tool=%s error=%s", tool_name, exc)
            return f"ERROR calling {tool_name}: {str(exc)[:2000]}"

        return "ERROR: Unhandled tool dispatch."

    # ------------------------------------------------------------------
    # Workspace file/script tools (shared, sandboxed)
    # ------------------------------------------------------------------

    def _call_workspace_tool(
        self,
        tool_name: str,
        tool_input: Union[str, Dict[str, Any]],
        state: Optional[Any],
    ) -> str:
        root = self._workspace_root
        if not root or not root.is_dir():
            return (
                "ERROR: workspace file tools are unavailable for this session "
                "(SQL-only datasource with no on-disk workspace). Use run_sql and get_schema."
            )
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
    # Analysis-specific tools
    # ------------------------------------------------------------------

    def _needs_cleaning(
        self,
        tool_input: Union[str, Dict[str, Any]],
        df: pd.DataFrame,
        state: Optional[Any],
    ) -> str:
        """
        Stops the ReAct loop after returning a short string; the agent sets
        analysis_react_halt in session metadata for process_query to convert into HITL.
        """
        if state is None:
            return "ERROR: needs_cleaning requires a live session with state."
        if state.data_context and state.data_context.is_sql_datasource():
            return (
                "ERROR: needs_cleaning is for file-based datasets. For SQL sessions, use the "
                "dedicated data-cleaning path from chat or the orchestrator, then return here."
            )
        if state.metadata.get("user_ran_cleaning"):
            return (
                "The working dataset was already cleaned in this session (or a cleaned artifact is "
                "in use). Do not request cleaning again unless the user explicitly asked for another "
                "pass. Continue analysing the active dataset in the workspace."
            )
        if not self._quality_summary_fn:
            return "ERROR: needs_cleaning is not available (internal: quality fn missing)."
        if isinstance(tool_input, str):
            reason = tool_input or "Data quality issues detected during analysis."
        else:
            reason = str(
                (tool_input or {}).get("reason")
                or (tool_input or {}).get("message")
                or "Data quality issues detected during analysis."
            )
        quality = self._quality_summary_fn(df)
        state.metadata["analysis_react_halt"] = {
            "kind": "needs_cleaning_permission",
            "reason": reason,
            "quality_summary": quality,
        }
        return (
            "Request recorded: the session will ask the user for permission to run the "
            "data-cleaning sub-agent. After the user answers, you will continue from here."
        )

    async def _run_sql(
        self,
        query: str,
        df: pd.DataFrame,
        state: Optional[Any],
    ) -> str:
        """Run a SELECT against Postgres pushdown or the session DuckDB snapshot."""
        if not query.strip():
            return "ERROR: No query provided to run_sql."

        # Postgres path — use sql_agent when available and state has a temp table.
        if (
            self._sql_agent
            and state
            and state.data_context
            and state.data_context.is_sql_datasource()
        ):
            try:
                frames = await self._sql_agent.run_attached_sql_frames(state, query)
                return _format_sql_frames(frames)
            except Exception as exc:
                return f"SQL ERROR: {str(exc)[:2000]}"

        if state and state.data_context:
            from execution.session_plane import PlaneKind, resolve_data_plane  # noqa: PLC0415

            try:
                plane = await asyncio.to_thread(resolve_data_plane, state, state.data_context)
                if plane.kind == PlaneKind.DUCKDB and plane.kernel is not None:
                    relation_ref = f'"{plane.relation_name}"'
                    query = query.replace("{table}", relation_ref)
                    try:
                        statements = parse_read_only_statements(query)
                    except ReadOnlySQLError as exc:
                        return f"SQL ERROR: {exc}"
                    scope_note = (
                        f" Source snapshot: {plane.scope.value}"
                        + (f", limit {plane.row_limit:,}" if plane.row_limit else "")
                        + (", source may be truncated." if plane.truncated else ".")
                    )
                    parts = []
                    total = len(statements)
                    for index, stmt in enumerate(statements, start=1):
                        result = await asyncio.to_thread(plane.kernel.query, stmt)
                        preview = result.to_pandas().head(_PREVIEW_ROWS)
                        extra = scope_note if index == 1 else ""
                        parts.append(
                            _format_one_query_result(
                                row_count=result.row_count,
                                preview=preview,
                                truncated=result.truncated,
                                extra=extra,
                                index=index,
                                total=total,
                            )
                        )
                    return _truncate("\n\n".join(parts))
            except Exception as exc:
                return f"SQL ERROR: {str(exc)[:2000]}"

        return "run_sql is unavailable because no analytical relation is loaded."

    async def _run_pipeline(self, tool_input: Any, state: Optional[Any]) -> str:
        """Run a read-only MongoDB aggregation against the active working copy.

        The pipeline guard runs inside the runner, so a model-authored ``$out``
        or ``$where`` is refused there rather than trusted here.
        """
        import asyncio  # noqa: PLC0415
        import json  # noqa: PLC0415

        if not state or not state.data_context:
            return "ERROR: No MongoDB collection is connected."
        meta = state.data_context.metadata or {}
        if meta.get("source") != "mongodb":
            return "ERROR: run_pipeline is only available for MongoDB sources."

        raw = tool_input if isinstance(tool_input, str) else tool_input.get("pipeline", "")
        if isinstance(raw, (list, tuple)):
            stages = list(raw)
        else:
            try:
                stages = json.loads(raw or "[]")
            except json.JSONDecodeError as exc:
                return f"ERROR: pipeline must be a JSON array of stages ({exc})."
        if not isinstance(stages, list):
            return "ERROR: pipeline must be a JSON array of stages."

        from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415
        from agents.mongo_agent.pipeline_guard import PipelineGuardError  # noqa: PLC0415
        from agents.mongo_agent.runner import MongoRunnerError  # noqa: PLC0415

        database = meta.get("temp_schema") or meta.get("database")
        collection = meta.get("temp_table") or meta.get("collection") or meta.get("table")
        if not collection:
            return "ERROR: No MongoDB collection is resolved for this session."
        try:
            runner = MongoScratchRunner.from_meta(meta)
            docs = await asyncio.to_thread(
                runner.run_pipeline,
                str(collection),
                stages + [{"$limit": 201}],
                database=str(database),
                allow_other_collections=False,
                allow_writes=False,
            )
        except PipelineGuardError as exc:
            return f"REFUSED: {exc}"
        except MongoRunnerError as exc:
            return f"PIPELINE ERROR: {str(exc)[:2000]}"

        if not docs:
            return "Pipeline returned no documents."
        from datasources.mongodb import flatten_documents  # noqa: PLC0415

        frame = flatten_documents(docs[:200])
        preview = frame.to_string(index=False, max_rows=50)
        suffix = f"\n... {len(docs)} documents returned" if len(docs) > 50 else ""
        return f"{preview}{suffix}"

    async def _sql_write_temp(
        self,
        tool_input: Union[str, Dict[str, Any]],
        state: Optional[Any],
    ) -> str:
        """Mutate the session's stych_temp working table (SQL sessions only)."""
        if not (
            self._sql_agent
            and state
            and state.data_context
            and state.data_context.is_sql_datasource()
        ):
            return (
                "sql_write_temp is only available when a PostgreSQL datasource is connected. "
                "Use run_script with pandas on the active dataset file instead."
            )
        if isinstance(tool_input, str):
            sql = tool_input
        else:
            sql = str((tool_input or {}).get("sql") or (tool_input or {}).get("query") or "")
        return await execute_sql_write_temp(self._sql_agent, state, sql)

    def _get_schema(self, df: pd.DataFrame) -> str:
        """Return column names, dtypes, null counts and sample values.

        For SQL sessions, return the real Postgres table schema (column names, DB types,
        row count). Otherwise read from the synced working_dataframe (kept consistent with
        the canonical data.parquet), so the columns shown match what scripts will load.
        """
        if self._sql_schema_text:
            return self._sql_schema_text

        _MAX_SCHEMA_COLS = 60
        lines = ["Schema (matches the active dataset in the workspace):"]
        null_counts = df.isnull().sum()
        for col in df.columns[:_MAX_SCHEMA_COLS]:
            dtype = str(df[col].dtype)
            nulls = int(null_counts[col])
            nunique = int(df[col].nunique(dropna=True))
            null_note = f", {nulls} nulls" if nulls else ""
            # Sample up to 5 unique non-null values for object columns; clip each
            # value so a wide cell (embedded JSON, long text) can't flood the loop.
            if pd.api.types.is_object_dtype(df[col]):
                samples = [str(v)[:80] for v in df[col].dropna().unique()[:5]]
                sample_note = f", samples={samples}"
            else:
                sample_note = ""
            lines.append(f"  {col}: {dtype} ({nunique} unique{null_note}{sample_note})")
        if len(df.columns) > _MAX_SCHEMA_COLS:
            lines.append(f"  … and {len(df.columns) - _MAX_SCHEMA_COLS} more columns")
        lines.append(f"\nTotal: {len(df)} rows × {len(df.columns)} columns")
        return _truncate("\n".join(lines), 8000)

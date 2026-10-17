"""
IntegrationAgent — connects Stych to external services via MCP servers.

The agent runs a ReAct loop with the following tools:
  list_workspace_files        — list files saved by analysis/cleaning agents
  read_workspace_file         — read a workspace file to pass to external services
  fetch_opendata_to_workspace — download a TryOpenData dataset directly to workspace CSV
  write_csv_to_spreadsheet    — export a workspace CSV straight to Google Sheets
  list_mcp_servers            — show available integrations and connection status
  list_mcp_tools              — fetch tool schemas from a specific MCP server
  call_mcp_tool               — call a specific tool on an MCP server

Credentials are never visible to the LLM; MCPClientManager injects them
internally from the database.

Tool implementations live in integrations/ (not here):
  integrations.tools_schema   — tool schemas and system prompt
  integrations.workspace_tools — list/read workspace files
  integrations.opendata_parser — TryOpenData response parser
  integrations.opendata_tools  — TryOpenData paginated fetch
  integrations.sheets_tools    — Google Sheets direct CSV export
  integrations.mcp_tools       — MCP server discovery and execution
  integrations.mcp_client      — per-turn MCP connection manager
  integrations.registry        — MCP server registry
  integrations.credential_store — per-user encrypted credential store
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agents.progress import _active_parent_part_id
from llm.infer import LLMRouter
from llm.models import Message
from mcp_infra.mcp_client import MCPClientManager
from mcp_infra.registry import MCP_REGISTRY
from react_engine.loop import (
    output_cap_from_config,
    run_agent_loop,
    transcript_budget_from_config,
)
from react_engine.schema import ReactStepRecord
from react_engine.toolkit import Toolkit
from tools.integration_tools import _INTEGRATION_TOOLS, _SYSTEM_PROMPT
from tools.mcp_tools import handle_call_mcp_tool, handle_list_mcp_servers, handle_list_mcp_tools
from tools.opendata_tools import handle_fetch_opendata
from tools.sheets_tools import (
    handle_read_spreadsheet_to_workspace,
    handle_write_csv_to_spreadsheet,
)
from tools.workspace_tools import handle_list_workspace_files, handle_read_workspace_file
from tracing.trace_parts import emit_react_part

if TYPE_CHECKING:
    from agents.progress import ProgressEmitter
    from agents.state import AgentState

logger = logging.getLogger(__name__)

MAX_STEPS = 20


class IntegrationAgent:
    """
    ReAct agent that discovers and calls external MCP server tools.

    One instance is created per OrchestratorAgent and reused across turns.
    A fresh MCPClientManager is created per execute() call so connections
    are properly cleaned up after each agent turn.
    """

    def __init__(self, llm_router: LLMRouter, config: Optional[Dict[str, Any]] = None):
        self.llm_router = llm_router
        self._config = config or {}

    async def execute(
        self,
        task: str,
        state: "AgentState",
        session_id: Optional[str] = None,
        progress_emitter: Optional["ProgressEmitter"] = None,
        app_user_id: Optional[str] = None,
        creds_map: Optional[Dict[str, Dict[str, str]]] = None,
        enabled_integrations: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Run the integration ReAct loop for the given task.

        Parameters
        ----------
        task:
            Natural language description of what the user wants to do.
        state:
            Current agent state — used to resolve workspace path and user_id.
        session_id:
            Used for Langfuse correlation.
        progress_emitter:
            Optional SSE emitter for live step events.
        app_user_id:
            DB app_user_id for loading credentials. Falls back to
            state.metadata["user_id"] if not provided.
        creds_map:
            Optional pre-built credentials map keyed by server_id. When provided,
            skips DB credential loading — useful for testing and direct invocations.
        enabled_integrations:
            Optional list of server_ids to enable (e.g. ["google_sheets", "opendata"]).
            When None or empty, all connected integrations are available.
        """
        uid = app_user_id or (state.metadata or {}).get("user_id") or ""

        if creds_map is None:
            creds_map = (
                _load_all_credentials(uid, filter_server_ids=enabled_integrations) if uid else {}
            )

        return await self._execute_unified(
            task=task, state=state, creds_map=creds_map, progress_emitter=progress_emitter
        )

    async def _execute_unified(
        self,
        *,
        task: str,
        state: "AgentState",
        creds_map: Dict[str, Dict[str, str]],
        progress_emitter: Optional["ProgressEmitter"],
    ) -> Dict[str, Any]:
        """
        Integration ReAct loop on the unified engine: append-only native-tool
        transcript (no fake-user messages), one tool result per ``role:"tool"``
        message correlated by ``tool_call_id``. No HITL — the integration agent
        never pauses for user input.
        """
        mcp_client = MCPClientManager()
        max_steps = int((self._config.get("agents") or {}).get("max_iterations", MAX_STEPS))
        collected: List[Dict[str, Any]] = []

        logger.info("[Integration] starting (unified) — task=%.120s", task)

        async def _tool_executor(name: str, args: Any, _call_id: str) -> str:
            try:
                return await _execute_tool(
                    action=name,
                    inp=args,
                    creds_map=creds_map,
                    mcp_client=mcp_client,
                    state=state,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("IntegrationAgent tool error: %s", exc)
                return f"Error executing {name}: {exc}"

        toolkit = Toolkit.from_schemas(
            _INTEGRATION_TOOLS,
            lambda name, args: _tool_executor(name, args, ""),
            known_names={
                "list_workspace_files",
                "read_workspace_file",
                "fetch_opendata_to_workspace",
                "write_csv_to_spreadsheet",
                "read_spreadsheet_to_workspace",
                "list_mcp_servers",
                "list_mcp_tools",
                "call_mcp_tool",
            },
            parallel_safe={"list_workspace_files", "list_mcp_servers", "list_mcp_tools"},
        )

        async def _emitter(rec: ReactStepRecord) -> None:
            if rec.phase in ("end", "final"):
                collected.append(
                    {
                        "step": rec.step,
                        "agent": "integration",
                        "phase": rec.phase,
                        "thought": rec.thought,
                        "action": rec.action,
                        "input": rec.input_preview,
                        "observation": rec.observation_preview,
                    }
                )
            await emit_react_part(
                progress_emitter, "integration", rec, parent_id=_active_parent_part_id.get()
            )

        async def _finalizer(text: str, _transcript: List[Message]) -> Dict[str, Any]:
            return {
                "status": "completed",
                "response": text or "Integration task completed.",
                "metadata": {"reasoning_trace": collected},
            }

        transcript = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=task),
        ]
        try:
            result = await run_agent_loop(
                llm_router=self.llm_router,
                agent_name="integration",
                toolkit=toolkit,
                transcript=transcript,
                max_steps=max_steps,
                temperature=0.3,
                output_token_cap=output_cap_from_config(self._config),
                transcript_token_budget=transcript_budget_from_config(self._config),
                emitter=_emitter,
                finalizer=_finalizer,
            )
        finally:
            await mcp_client.close_all()

        # Engine returns "partial" on step-budget exhaustion; map to the legacy
        # error contract the orchestrator/UI expect for this agent.
        if result.get("status") == "partial":
            return {
                "status": "error",
                "response": "Integration agent exceeded the maximum number of steps.",
                "metadata": {"reasoning_trace": collected},
            }
        return {
            "status": result.get("status", "completed"),
            "response": result.get("response", ""),
            "metadata": {"reasoning_trace": collected},
        }


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


async def _execute_tool(
    action: str,
    inp: Any,
    creds_map: Dict[str, Dict[str, str]],
    mcp_client: MCPClientManager,
    state: Optional["AgentState"] = None,
) -> str:
    if action == "list_workspace_files":
        return handle_list_workspace_files(state)
    if action == "read_workspace_file":
        return handle_read_workspace_file(inp, state)
    if action == "fetch_opendata_to_workspace":
        return await handle_fetch_opendata(inp, creds_map, state)
    if action == "write_csv_to_spreadsheet":
        return await handle_write_csv_to_spreadsheet(inp, creds_map, state, mcp_client)
    if action == "read_spreadsheet_to_workspace":
        return await handle_read_spreadsheet_to_workspace(inp, creds_map, state, mcp_client)
    if action == "list_mcp_servers":
        return handle_list_mcp_servers(creds_map)
    if action == "list_mcp_tools":
        return await handle_list_mcp_tools(inp, creds_map, mcp_client)
    if action == "call_mcp_tool":
        return await handle_call_mcp_tool(inp, creds_map, mcp_client, state)
    return (
        f"Error: Unknown tool '{action}'. "
        f"Valid tools: list_workspace_files, read_workspace_file, "
        f"fetch_opendata_to_workspace, write_csv_to_spreadsheet, "
        f"read_spreadsheet_to_workspace, "
        f"list_mcp_servers, list_mcp_tools, call_mcp_tool."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_all_credentials(
    app_user_id: str, filter_server_ids: Optional[List[str]] = None
) -> Dict[str, Dict[str, str]]:
    """
    Load credentials for registry servers for this user.

    Parameters
    ----------
    app_user_id:
        App user UUID.
    filter_server_ids:
        Optional list of server_ids to load. When None or empty, all connected integrations
        are loaded (legacy behavior). When provided, only the specified integrations are loaded.
    """
    from database.connection import SessionLocal  # noqa: PLC0415
    from helpers.oauth_token_refresh import maybe_refresh_oauth_credentials  # noqa: PLC0415

    from .credential_store import get_credentials  # noqa: PLC0415

    result: Dict[str, Dict[str, str]] = {}
    db = SessionLocal()
    try:
        # Use `is not None` — an empty list explicitly means "no integrations"
        # whereas None means "all connected integrations" (legacy / all-selected).
        servers_to_check = (
            [sid for sid in filter_server_ids if sid in MCP_REGISTRY]
            if filter_server_ids is not None
            else list(MCP_REGISTRY.keys())
        )
        for server_id in servers_to_check:
            creds = get_credentials(db, app_user_id, server_id)
            if creds is not None:
                creds = maybe_refresh_oauth_credentials(db, app_user_id, server_id, creds)
                result[server_id] = creds
    except Exception:
        logger.exception("Failed to load MCP credentials for user=%s", app_user_id)
    finally:
        db.close()
    return result

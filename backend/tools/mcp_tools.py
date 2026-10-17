"""
MCP server discovery and tool execution for the IntegrationAgent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from mcp_infra.mcp_client import MCPClientManager
from mcp_infra.registry import MCP_REGISTRY
from mcp_infra.schema_patches import TOOL_SCHEMA_PATCHES

logger = logging.getLogger(__name__)


def _unwrap_exception(exc: BaseException) -> str:
    """Unwrap anyio ExceptionGroup to surface the real underlying error."""
    cause: BaseException = exc
    while hasattr(cause, "exceptions") and cause.exceptions:  # type: ignore[attr-defined]
        cause = cause.exceptions[0]  # type: ignore[attr-defined]
    return str(cause)


# MCP servers return arbitrary external payloads (spreadsheet ranges, API dumps)
# with no inherent size bound — the single biggest uncapped path into the loop
# transcript. Oversized results are persisted to the workspace and replaced with
# a preview + file handle the agent can page through via read_workspace_file.
_MAX_MCP_RESULT_CHARS = 8_000
_MCP_PREVIEW_CHARS = 2_000


def _cap_mcp_result(result: str, tool_name: str, state: Any) -> str:
    if len(result) <= _MAX_MCP_RESULT_CHARS:
        return result

    from tools.workspace_tools import _get_workspace_path  # noqa: PLC0415

    workspace = _get_workspace_path(state)
    if workspace is not None:
        try:
            import uuid  # noqa: PLC0415

            out_dir = workspace / "mcp_results"
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_name)[:40]
            fname = f"{safe_name}_{uuid.uuid4().hex[:8]}.txt"
            (out_dir / fname).write_text(result, encoding="utf-8")
            return (
                f"[Result too large for context: {len(result):,} chars. Full output saved to "
                f"workspace file 'mcp_results/{fname}' — use read_workspace_file (with offset) "
                f"to page through it, or run_script to process it.]\n\n"
                f"Preview (first {_MCP_PREVIEW_CHARS} chars):\n{result[:_MCP_PREVIEW_CHARS]}"
            )
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning("Failed to persist oversized MCP result: %s", exc)
    omitted = len(result) - _MAX_MCP_RESULT_CHARS
    return result[:_MAX_MCP_RESULT_CHARS] + f"\n… [truncated, {omitted:,} chars omitted]"


def _format_object_inline(schema: Dict[str, Any]) -> str:
    """Render an object JSON-schema as a compact inline literal, e.g. {title: string}."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return ""
    required = set(schema.get("required") or [])
    parts: List[str] = []
    for k, v in props.items():
        v = v if isinstance(v, dict) else {}
        t = v.get("type", "any")
        star = "*" if k in required else ""
        parts.append(f"{k}{star}: {t}")
    return "{" + ", ".join(parts) + "}"


def _format_tool_params(input_schema: Any, *, enrich_param_descriptions: bool = False) -> str:
    """Render an MCP tool's JSON-schema as a compact signature string.

    Returns e.g. ``name*: string, slides*: {title*: string, content*: string}[]``
    where ``*`` marks required parameters. When ``enrich_param_descriptions`` is
    True, appends short per-parameter descriptions from the schema.
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "no parameters"
    required = set(input_schema.get("required") or [])
    parts: List[str] = []
    for pname, pinfo in props.items():
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        enum = pinfo.get("enum")
        if isinstance(enum, list) and enum:
            ptype = "|".join(str(e) for e in enum[:6])
        else:
            ptype = pinfo.get("type", "any")
            if ptype == "array":
                items = pinfo.get("items") or {}
                item_type = items.get("type") if isinstance(items, dict) else None
                if item_type == "object":
                    inline = _format_object_inline(items)
                    ptype = f"{inline}[]" if inline else "object[]"
                else:
                    ptype = f"{item_type}[]" if item_type else "array"
        star = "*" if pname in required else ""
        segment = f"{pname}{star}: {ptype}"
        if enrich_param_descriptions:
            desc = (pinfo.get("description") or "").strip()
            if desc:
                if len(desc) > 120:
                    desc = desc[:117] + "…"
                segment = f"{segment} — {desc}"
        parts.append(segment)
    return ", ".join(parts)


def handle_list_mcp_servers(creds_map: Dict[str, Dict[str, str]]) -> str:
    lines = []
    for server_id, server_def in MCP_REGISTRY.items():
        connected = server_id in creds_map
        if connected:
            creds = creds_map[server_id]
            missing = [k for k in server_def.required_credentials if not creds.get(k)]
            status = "connected_missing_credentials" if missing else "connected"
        else:
            status = "not_connected"
        lines.append(
            f"- {server_id}: {server_def.name} | status={status} | "
            f"description={server_def.description}"
        )
    return "\n".join(lines) if lines else "No MCP servers registered."


async def handle_list_mcp_tools(
    inp: Any,
    creds_map: Dict[str, Dict[str, str]],
    mcp_client: MCPClientManager,
) -> str:
    server_id = inp.get("server_id", "") if isinstance(inp, dict) else str(inp)
    server_def = MCP_REGISTRY.get(server_id)
    if server_def is None:
        return f"Error: Unknown server_id '{server_id}'. Use list_mcp_servers to see valid IDs."

    if server_id not in creds_map:
        return (
            f"Error: {server_def.name} is not connected. "
            f"The user must go to Settings → Integrations and connect it first."
        )

    creds = creds_map.get(server_id, {})
    missing = [k for k in server_def.required_credentials if not creds.get(k)]
    if missing:
        return (
            f"Error: {server_def.name} is connected but missing required credentials: "
            f"{missing}. The user must go to Settings → Integrations and add them."
        )

    try:
        tools = await mcp_client.list_tools(server_def, creds)
        if not tools:
            return f"Server '{server_id}' connected but returned no tools."
        lines = [
            f"Tools on {server_def.name} (call via call_mcp_tool; "
            f"* marks a required argument — use these exact names):"
        ]
        for t in tools:
            enrich = bool(t.get("stych_enrich_params"))
            params = _format_tool_params(
                t.get("inputSchema") or {}, enrich_param_descriptions=enrich
            )
            desc = (t.get("description") or "").strip().split("\n")[0]
            if enrich:
                desc_limit = 200
            else:
                desc_limit = 100
            if len(desc) > desc_limit:
                desc = desc[: desc_limit - 1] + "…"
            header = f"  - {t['name']}({params})"
            lines.append(f"{header} — {desc}" if desc else header)
        return "\n".join(lines)
    except Exception as exc:
        error = f"Error connecting to {server_def.name}: {_unwrap_exception(exc)}"
        known = sorted(TOOL_SCHEMA_PATCHES.get(server_id) or {})
        if known:
            error += (
                f" Tool discovery failed, but previously known tool names for this "
                f"server include: {', '.join(known)}. You may try call_mcp_tool with "
                f"one of these if it fits the task, or tell the user discovery failed."
            )
        return error


async def handle_call_mcp_tool(
    inp: Any,
    creds_map: Dict[str, Dict[str, str]],
    mcp_client: MCPClientManager,
    state: Any = None,
) -> str:
    server_id = inp.get("server_id", "") if isinstance(inp, dict) else ""
    tool_name = inp.get("tool_name", "") if isinstance(inp, dict) else ""
    arguments = inp.get("arguments", {}) if isinstance(inp, dict) else {}

    server_def = MCP_REGISTRY.get(server_id)
    if server_def is None:
        return f"Error: Unknown server_id '{server_id}'."

    if server_id not in creds_map:
        return (
            f"Error: {server_def.name} is not connected. "
            f"Go to Settings → Integrations to connect it first."
        )

    creds = creds_map.get(server_id, {})
    missing = [k for k in server_def.required_credentials if not creds.get(k)]
    if missing:
        return (
            f"Error: {server_def.name} is connected but missing required credentials: "
            f"{missing}. The user must go to Settings → Integrations and add them."
        )

    try:
        result = await mcp_client.call_tool(server_def, tool_name, arguments, creds)
    except Exception as exc:
        return f"Error calling {tool_name} on {server_def.name}: {_unwrap_exception(exc)[:2000]}"

    # If the MCP server returned a schema validation error, enrich the message with
    # the actual parameter names so the LLM can self-correct without an extra round-trip.
    if "Invalid input" in result or "expected string, received undefined" in result:
        try:
            tools = await mcp_client.list_tools(server_def, creds)
            match = next((t for t in tools if t["name"] == tool_name), None)
            if match:
                params = _format_tool_params(
                    match.get("inputSchema") or {},
                    enrich_param_descriptions=bool(match.get("stych_enrich_params")),
                )
                if params and params != "no parameters":
                    return (
                        f"{result}\n\n"
                        f"Correct signature for {tool_name}: ({params})\n"
                        f"Retry with these exact parameter names (* = required)."
                    )
        except Exception:
            pass

    return _cap_mcp_result(result, tool_name, state)

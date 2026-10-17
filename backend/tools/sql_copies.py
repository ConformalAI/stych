"""
Shared working-copy lifecycle tools for SQL sessions.

Both the cleaning and analysis ReAct registries expose these through one
implementation, so every subagent gets identical branch/switch/drop/list/diff
behavior over the session's ``stych_temp`` working copies.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Union

logger = logging.getLogger(__name__)

WORKING_COPY_TOOL_NAMES = frozenset(
    {
        "create_working_copy",
        "switch_working_copy",
        "drop_working_copy",
        "list_working_copies",
        "diff_working_copies",
    }
)

WORKING_COPY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_working_copy",
            "description": (
                "Create a NEW named working copy and make it active. By default it branches "
                "from the user's original source table (a fresh start); pass from=<copy name> "
                "to branch from an existing copy instead (keeps its changes). Use when the "
                "user wants to start over, try a different approach, or checkpoint before "
                "risky changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Short name for the copy (letters/digits/_/-, e.g. 'imputed', "
                            "'v2'). Auto-generated when omitted."
                        ),
                    },
                    "from": {
                        "type": "string",
                        "description": "'source' (default) or the name of an existing copy.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_working_copy",
            "description": (
                "Make an existing working copy the ACTIVE one — the table that {table}, "
                "reads/writes, and the user's data preview/diff all point at."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop_working_copy",
            "description": (
                "Permanently delete a working copy (its table and its changes). Only call "
                "this when the user explicitly asks to delete/discard a copy. If the active "
                "copy is dropped, the most recent remaining copy becomes active."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_working_copies",
            "description": (
                "List this session's working copies: name, parent lineage, row count, "
                "created time, and which one is active."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff_working_copies",
            "description": (
                "Compare two versions of the data and return the change summary the user "
                "sees in the Diff tab: rows added/removed/changed and per-column changed "
                "cells. base/target accept 'source' (the original table) or a copy name. "
                "Defaults: base='source', target=the active copy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "base": {"type": "string"},
                    "target": {"type": "string"},
                },
            },
        },
    },
]


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 — observation string for the LLM
        return str(value)


def _arg(tool_input: Union[str, Dict[str, Any]], *keys: str) -> str:
    if isinstance(tool_input, str):
        return tool_input.strip()
    if isinstance(tool_input, dict):
        for k in keys:
            v = tool_input.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


async def execute_working_copy_tool(
    sql_agent: Any,
    state: Any,
    tool_name: str,
    tool_input: Union[str, Dict[str, Any]],
) -> str:
    """Dispatch one working-copy lifecycle tool; returns a text observation."""
    # Lazy imports: the agents package pulls in the tool registries at init,
    # so a top-level import here would be circular.
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415

    if not sql_agent or not state or not state.data_context:
        return "ERROR: SQL datasource is not available."
    if not state.data_context.is_sql_datasource():
        return (
            "ERROR: working-copy tools are only available when a SQL "
            "datasource with a scratch working copy is connected."
        )

    try:
        if tool_name == "create_working_copy":
            name = _arg(tool_input, "name") or None
            parent = _arg(tool_input, "from", "parent", "source") or wc.SOURCE_PARENT
            entry = await sql_agent.create_working_copy(state, name=name, parent=parent)
            runner = sql_agent._runner_for(state)
            quoted = runner.quote_relation(runner.temp_schema, entry["table"])
            return (
                f"Created working copy {entry['name']!r} from {parent!r} "
                f"(table {quoted}) — it is now the active copy."
            )

        if tool_name == "switch_working_copy":
            name = _arg(tool_input, "name")
            if not name:
                return "ERROR: switch_working_copy requires {name}."
            entry = await sql_agent.switch_working_copy(state, name)
            runner = sql_agent._runner_for(state)
            quoted = runner.quote_relation(runner.temp_schema, entry["table"])
            return f"Active working copy is now {entry['name']!r} " f"(table {quoted})."

        if tool_name == "drop_working_copy":
            name = _arg(tool_input, "name")
            if not name:
                return "ERROR: drop_working_copy requires {name}."
            entry = await sql_agent.drop_working_copy(state, name)
            meta = state.data_context.metadata
            active = wc.active_copy(meta)
            now_active = f"{active['name']!r} is now active." if active else "No copies remain."
            return f"Dropped working copy {entry['name']!r}. {now_active}"

        if tool_name == "list_working_copies":
            copies = await sql_agent.list_working_copies(state)
            if not copies:
                return "No working copies yet — one is created on the first data operation."
            return _json({"working_copies": copies})

        if tool_name == "diff_working_copies":
            return await _diff(sql_agent, state, tool_input)

    except wc.WorkingCopyError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 — observation string for the LLM
        logger.warning("[working-copy tool] %s failed: %s", tool_name, exc)
        return f"ERROR: {exc}"

    return f"ERROR: Unknown working-copy tool '{tool_name}'."


async def _diff(sql_agent: Any, state: Any, tool_input: Union[str, Dict[str, Any]]) -> str:
    import asyncio  # noqa: PLC0415

    from agents.sql_agent import working_copies as wc  # noqa: PLC0415
    from api.sql_diff_service import build_sql_diff_page  # noqa: PLC0415
    from datasources import adapters as table_adapters  # noqa: PLC0415

    ctx = state.data_context
    meta = ctx.metadata
    wc.adopt_legacy(meta)

    def _variant(label: str) -> str:
        if label in ("", "source", "base", "original"):
            pinned = wc.pinned_base(meta)
            return f"copy:{pinned['name']}" if pinned else "base"
        if wc.get_copy(meta, label) is None:
            raise wc.WorkingCopyError(f"No working copy named {label!r}.")
        return f"copy:{label}"

    base_label = _arg(tool_input, "base") or "source"
    active = wc.active_copy(meta)
    target_label = _arg(tool_input, "target") or (active["name"] if active else "")
    if not target_label:
        return "ERROR: No working copy exists yet — nothing to diff against."
    if base_label == target_label:
        return "ERROR: base and target are the same table."

    # Ensure the active copy (and registry) exist before resolving adapters.
    await sql_agent.ensure_temp_table(state)
    if meta.get("source") == "mongodb":
        from api.mongo_diff_service import (  # noqa: PLC0415
            build_mongo_diff_page,
            resolve_diff_target,
        )

        runner = sql_agent._runner_for(state)
        base_db, base_collection = resolve_diff_target(
            meta, _variant(base_label), runner.temp_schema
        )
        target_db, target_collection = resolve_diff_target(
            meta, _variant(target_label), runner.temp_schema
        )
        if base_db != target_db:
            return "ERROR: MongoDB diff sides must be in the same scratch database."

        page = await asyncio.to_thread(
            build_mongo_diff_page,
            runner,
            database=base_db,
            base_collection=base_collection,
            target_collection=target_collection,
            page=1,
            page_size=1,
        )
        changed_columns = [
            {k: c.get(k) for k in ("name", "status", "changed_cells", "note")}
            for c in page["columns"]
            if c.get("changed_cells") or c.get("status") != "common"
        ]
        return _json(
            {
                "base": base_label,
                "target": target_label,
                "summary": page["summary"],
                "changed_columns": changed_columns,
            }
        )

    base_adapter = table_adapters.resolve_adapter(ctx, _variant(base_label))
    target_adapter = table_adapters.resolve_adapter(ctx, _variant(target_label))

    def _run():
        return build_sql_diff_page(
            base_adapter,
            target_adapter,
            session_id=state.session_id,
            page=1,
            page_size=1,
        )

    page = await asyncio.to_thread(_run)
    changed_columns = [
        {k: c.get(k) for k in ("name", "status", "changed_cells", "note")}
        for c in page["columns"]
        if c.get("changed_cells") or c.get("status") != "common"
    ]
    return _json(
        {
            "base": base_label,
            "target": target_label,
            "summary": page["summary"],
            "changed_columns": changed_columns,
        }
    )

"""
Workspace file tools for the IntegrationAgent.

Provides access to CSV/text files saved by the analysis and cleaning agents
so their outputs can be forwarded to external services.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agents.state import AgentState

logger = logging.getLogger(__name__)


def _get_workspace_path(state: Optional["AgentState"]) -> Optional[Path]:
    """
    Resolve the session workspace directory — same path used by analysis/cleaning
    agents via WorkspaceManager.get_or_create(session_id, user_id).
    Returns None if the workspace cannot be determined.
    """
    if not state:
        return None
    try:
        from workspace.manager import WorkspaceManager  # noqa: PLC0415

        session_id = getattr(state, "session_id", None)
        if not session_id:
            return None
        user_id = (state.metadata or {}).get("user_id", "anonymous")
        mgr = WorkspaceManager()
        result = mgr.get_or_create(session_id, user_id=user_id)
        return Path(result) if result else None
    except Exception:
        logger.debug("Could not resolve workspace path", exc_info=True)
        return None


def handle_list_workspace_files(state: Optional["AgentState"]) -> str:
    workspace = _get_workspace_path(state)
    if not workspace or not workspace.is_dir():
        return (
            "No workspace files found for this session. "
            "The analysis/cleaning agent has not saved any files yet."
        )
    files = sorted(workspace.iterdir())
    if not files:
        return (
            "Workspace exists but is empty. "
            "Ask the analysis agent to save its results to the workspace first."
        )
    lines = [f"Workspace files ({len(files)} total):"]
    for f in files:
        size_kb = f.stat().st_size / 1024 if f.is_file() else 0
        lines.append(f"  - {f.name}  ({size_kb:.1f} KB)")
    return "\n".join(lines)


def handle_read_workspace_file(inp: Any, state: Optional["AgentState"]) -> str:
    path_arg = inp.get("path", "") if isinstance(inp, dict) else str(inp)
    workspace = _get_workspace_path(state)
    if not workspace:
        return "Error: cannot determine workspace path for this session."
    full_path = (workspace / path_arg).resolve()
    try:
        full_path.relative_to(workspace.resolve())
    except ValueError:
        return f"Error: path '{path_arg}' is outside the workspace."
    if not full_path.exists():
        return (
            f"Error: file '{path_arg}' not found in workspace. "
            "Use list_workspace_files to see available files."
        )
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        total = len(content)
        cap = 20_000
        offset = 0
        if isinstance(inp, dict):
            try:
                offset = max(int(inp.get("offset", 0) or 0), 0)
            except (TypeError, ValueError):
                offset = 0
        chunk = content[offset : offset + cap]
        end = offset + len(chunk)
        if offset or end < total:
            chunk += (
                f"\n… [showing chars {offset:,}–{end:,} of {total:,}; "
                f"pass offset={end} to continue]"
            )
        logger.debug("[Integration] read_workspace_file %s (%d chars)", path_arg, len(chunk))
        return chunk
    except Exception as exc:
        return f"Error reading '{path_arg}': {str(exc)[:2000]}"

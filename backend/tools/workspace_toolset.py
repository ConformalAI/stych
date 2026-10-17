"""
Shared, sandboxed workspace tools for all ReAct subagents.

Exposes primitive, file-based tools — ``list_files``, ``read_file``, ``write_file``,
``edit_file``, ``delete_file``, ``run_script`` — that operate on a per-session
workspace root. The canonical dataset lives in the workspace as ``data.parquet``
(see ``workspace.manager.seed_workspace_data``); after any ``run_script`` the
toolset reloads that file into ``state.data_context.working_dataframe`` so downstream
consumers (graph/QA/frontend/snapshots) stay consistent without the agent ever
handling a magic ``df``/``result`` variable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

import pandas as pd

from agents.handoff import append_handoff_event
from workspace.manager import (
    _INTERNAL_FILES,
    list_workspace_artifacts,
    resolve_active_dataset,
)
from workspace.runner import WorkspaceSandboxRunner

logger = logging.getLogger(__name__)

_MAX_OBS_CHARS = 6000
_MAX_ERROR_TAIL = 2000
_MAX_STDOUT_HEAD = 3000

# Workspace file tools shared by every subagent.
WORKSPACE_FILE_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "run_script",
        "set_active_dataset",
    }
)


def _truncate(text: str, max_chars: int = _MAX_OBS_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… [truncated, {len(text) - max_chars} chars omitted]"


def _format_script_result(result: Dict[str, Any]) -> str:
    """Format a sandbox run result into a compact JSON observation for the agent.

    Errors are tail-truncated (keeping the most recent traceback lines) so the agent
    always sees the actual exception type and message.
    """
    error = result.get("error") or None
    if error and len(error) > _MAX_ERROR_TAIL:
        omitted = len(error) - _MAX_ERROR_TAIL
        error = f"[…{omitted} chars omitted…]\n" + error[-_MAX_ERROR_TAIL:]

    stdout = (result.get("stdout") or "").rstrip()
    if len(stdout) > _MAX_STDOUT_HEAD:
        stdout = (
            stdout[:_MAX_STDOUT_HEAD]
            + f"\n… [stdout truncated, {len(stdout) - _MAX_STDOUT_HEAD} chars omitted]"
        )

    stderr = (result.get("stderr") or "").rstrip()

    payload: Dict[str, Any] = {
        "success": result.get("success"),
        "stdout": stdout,
        "files": result.get("files", []),
    }
    if stderr:
        payload["stderr"] = stderr[:500]
    if error is not None:
        payload["error"] = error

    try:
        text = json.dumps(payload, default=str)
    except Exception:
        text = str(payload)
    return _truncate(text)


class WorkspaceToolset:
    """File + script tools bound to a sandbox runner; operate on a workspace root."""

    def __init__(self, runner: Optional[WorkspaceSandboxRunner] = None) -> None:
        self.runner = runner or WorkspaceSandboxRunner()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def _resolve_path(self, root: Path, path: str) -> Path:
        root = root.resolve()
        candidate = (root / path).resolve()
        if not str(candidate).startswith(str(root)):
            raise ValueError("Path traversal outside workspace is not allowed.")
        return candidate

    def _safe_resolve(self, root: Path, path: str) -> tuple[Optional[Path], Optional[str]]:
        """Resolve a workspace-relative path, returning (path, None) or (None, error)."""
        try:
            return self._resolve_path(root, path), None
        except ValueError as exc:
            return None, f"ERROR: {exc}"

    # ------------------------------------------------------------------
    # File tools
    # ------------------------------------------------------------------
    def list_files(self, root: Path) -> str:
        files = self.runner.list_files(root)
        return _truncate(json.dumps({"root": str(root), "files": files}, indent=2))

    def read_file(self, root: Path, tool_input: Union[str, Dict[str, Any]]) -> str:
        if isinstance(tool_input, str):
            path, start_line, max_lines = tool_input, 1, 200
        else:
            path = str(tool_input.get("path", "") or "")
            start_line = int(tool_input.get("start_line", 1) or 1)
            max_lines = int(tool_input.get("max_lines", 200) or 200)
        if not path:
            return "ERROR: Missing path for read_file."
        resolved, err = self._safe_resolve(root, path)
        if err:
            return err
        if not resolved.exists():
            return f"ERROR: File not found: {path}"
        lines = resolved.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines:
            return f"ERROR: File is empty or could not be read: {path}"
        start = max(1, start_line)
        end = min(len(lines), start - 1 + max_lines)
        excerpt = "\n".join(lines[start - 1 : end])
        if not excerpt.strip():
            return f"ERROR: File is empty or could not be read: {path}"
        return _truncate(excerpt)

    def write_file(self, root: Path, tool_input: Union[str, Dict[str, Any]]) -> str:
        if isinstance(tool_input, str):
            return "ERROR: write_file expects {path, content}."
        path = str(tool_input.get("path", "") or "")
        content = tool_input.get("content", "")
        if not path:
            return "ERROR: Missing path for write_file."
        resolved, err = self._safe_resolve(root, path)
        if err:
            return err
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(str(content), encoding="utf-8")
        return f"Wrote {resolved.relative_to(root.resolve())} ({len(str(content))} chars)."

    def edit_file(self, root: Path, tool_input: Union[str, Dict[str, Any]]) -> str:
        if isinstance(tool_input, str):
            return "ERROR: edit_file expects {path, old_string, new_string}."
        path = str(tool_input.get("path", "") or "")
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not path or old is None or new is None:
            return "ERROR: edit_file requires path, old_string, and new_string."
        resolved, err = self._safe_resolve(root, path)
        if err:
            return err
        if not resolved.is_file():
            return f"ERROR: File not found: {path}"
        text = resolved.read_text(encoding="utf-8", errors="ignore")
        count = text.count(str(old))
        if count == 0:
            return f"ERROR: old_string not found in {path}."
        if count > 1:
            return f"ERROR: old_string is not unique in {path} ({count} matches)."
        resolved.write_text(text.replace(str(old), str(new), 1), encoding="utf-8")
        return f"Edited {resolved.relative_to(root.resolve())}."

    def delete_file(self, root: Path, tool_input: Union[str, Dict[str, Any]]) -> str:
        path = tool_input if isinstance(tool_input, str) else str(tool_input.get("path", "") or "")
        if not path:
            return "ERROR: Missing path for delete_file."
        resolved, err = self._safe_resolve(root, path)
        if err:
            return err
        if not resolved.exists():
            return f"ERROR: File not found: {path}"
        if resolved.is_dir():
            return f"ERROR: {path} is a directory; delete_file only removes files."
        resolved.unlink()
        return f"Deleted {path}."

    def set_active_dataset(
        self,
        root: Path,
        tool_input: Union[str, Dict[str, Any]],
        state: Optional[Any] = None,
    ) -> str:
        """Promote a workspace file to be the session's working dataset.

        Use after writing a new dataframe file (e.g. ``titanic_engineered.csv``) to make
        it the active dataset that the rest of the app (schema, charts, snapshots, the
        user's view) reads — without forcing the agent to overwrite the original.
        """
        path = tool_input if isinstance(tool_input, str) else str(tool_input.get("path", "") or "")
        if not path:
            return "ERROR: Missing path for set_active_dataset."
        resolved, err = self._safe_resolve(root, path)
        if err:
            return err
        if not resolved.is_file():
            return f"ERROR: File not found: {path}"
        if resolved.suffix.lower() not in (".parquet", ".csv"):
            return "ERROR: active dataset must be a .parquet or .csv file."
        if state is None or not getattr(state, "data_context", None):
            return "ERROR: set_active_dataset requires a live session with a dataset."
        if state.data_context.is_sql_datasource():
            return "ERROR: set_active_dataset is for file-based sessions only."

        stem = resolved.stem
        try:
            if resolved.suffix.lower() == ".parquet":
                loaded = pd.read_parquet(resolved, engine="pyarrow")
            else:
                loaded = pd.read_csv(resolved)
                loaded.to_parquet(root / f"{stem}.parquet", index=False, engine="pyarrow")
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: could not load '{path}' as a dataset: {exc}"

        state.metadata["active_dataset"] = stem
        state.data_context.working_dataframe = loaded
        state.metadata["user_ran_cleaning"] = True
        state.metadata["readiness_cache_invalid"] = True
        state.metadata["workspace_canonical_file"] = str(root / f"{stem}.parquet")
        try:
            from agents.state import state_manager

            if getattr(state, "session_id", None):
                state_manager.mark_dirty(state.session_id)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_artifact_manifest(root, state)
        append_handoff_event(
            state,
            source_agent="workspace",
            action="set_active_dataset",
            details={
                "primary_artifact": resolved.name,
                "absolute_path": str(root / f"{stem}.parquet"),
                "workspace_root": str(root),
                "rows": int(len(loaded)),
                "columns": int(len(loaded.columns)),
            },
        )
        return (
            f"Active dataset is now '{resolved.name}' "
            f"({len(loaded)} rows × {len(loaded.columns)} columns). "
            "The app and the user now see this as the working dataset."
        )

    # ------------------------------------------------------------------
    # Script execution + canonical-dataframe sync
    # ------------------------------------------------------------------
    def run_script(
        self,
        root: Path,
        tool_input: Union[str, Dict[str, Any]],
        state: Optional[Any] = None,
    ) -> str:
        if isinstance(tool_input, str):
            code = tool_input
        else:
            code = tool_input.get("code") or ""
            path = tool_input.get("path")
            if not code and path:
                try:
                    resolved = self._resolve_path(root, str(path))
                    code = resolved.read_text(encoding="utf-8")
                except Exception as exc:
                    return f"ERROR: Failed to read script at path '{path}': {exc}"
        if not str(code).strip():
            return "ERROR: No code provided to run_script."

        pre = self._snapshot_files(root)
        try:
            result = self.runner.run(root, code)
        except ValueError as exc:
            # Sandbox policy violation (blocked import / forbidden call) — return a clean
            # observation so the agent can correct rather than raising.
            return _format_script_result({"success": False, "error": str(exc)})
        self._sync_after_run(root, state, pre)
        return _format_script_result(result)

    def _snapshot_files(self, root: Path) -> Dict[str, float]:
        """Map every user-visible workspace file → mtime. Basis for change detection."""
        snapshot: Dict[str, float] = {}
        if not root.is_dir():
            return snapshot
        for p in root.rglob("*"):
            if not p.is_file() or p.name in _INTERNAL_FILES:
                continue
            try:
                snapshot[str(p.relative_to(root))] = p.stat().st_mtime
            except OSError:
                continue
        return snapshot

    def _sync_after_run(
        self,
        root: Path,
        state: Optional[Any],
        pre_files: Dict[str, float],
    ) -> None:
        """Reconcile workspace state after a script: refresh the artifact manifest and,
        when the *active dataset* file changed, reload it into working_dataframe.

        Change detection is directory-wide (every created/modified/deleted file is
        captured) rather than keyed to two hardcoded filenames, so agent-authored
        outputs are never silently dropped.
        """
        if not state or not getattr(state, "data_context", None):
            return
        if state.data_context.is_sql_datasource():
            return

        post_files = self._snapshot_files(root)
        changed = {
            name
            for name, mt in post_files.items()
            if pre_files.get(name) is None or mt > pre_files[name]
        }

        # Always refresh the manifest so the browser/handoff reflect reality.
        self._refresh_artifact_manifest(root, state)

        active = resolve_active_dataset(root, state)
        if active is None:
            return
        parquet_path, csv_path, _stem = active
        parquet_rel = self._rel(root, parquet_path)
        csv_rel = self._rel(root, csv_path)

        loaded: Optional[pd.DataFrame] = None
        source: Optional[str] = None
        try:
            if parquet_rel in changed and parquet_path.is_file():
                loaded = pd.read_parquet(parquet_path, engine="pyarrow")
                source = parquet_path.name
            elif csv_rel in changed and csv_path.is_file():
                # Agent wrote a same-stem CSV; fold it into the parquet source of truth.
                loaded = pd.read_csv(csv_path)
                source = csv_path.name
                try:
                    loaded.to_parquet(parquet_path, index=False, engine="pyarrow")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workspace] active-dataset sync failed: %s", exc)
            return

        if loaded is None:
            return

        state.data_context.working_dataframe = loaded
        try:
            from agents.state import state_manager

            if getattr(state, "session_id", None):
                state_manager.mark_dirty(state.session_id)
        except Exception:  # noqa: BLE001
            pass
        state.metadata["user_ran_cleaning"] = True
        state.metadata["readiness_cache_invalid"] = True
        # Signal that the on-disk active-dataset file is authoritative — lets the analysis
        # agent reload it across handoffs/turns.
        state.metadata["workspace_canonical_file"] = str(parquet_path)
        append_handoff_event(
            state,
            source_agent="workspace",
            action="wrote_canonical_dataset",
            details={
                "primary_artifact": source,
                "absolute_path": str(parquet_path),
                "workspace_root": str(root),
                "rows": int(len(loaded)),
                "columns": int(len(loaded.columns)),
            },
        )

    @staticmethod
    def _rel(root: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(root.resolve()))
        except ValueError:
            return path.name

    def _refresh_artifact_manifest(self, root: Path, state: Optional[Any]) -> None:
        """Record every user-visible workspace file on the session so the file browser
        and downstream consumers can list agent-created outputs (not just the dataset)."""
        if not state:
            return
        try:
            state.metadata["workspace_artifacts"] = list_workspace_artifacts(root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workspace] manifest refresh failed: %s", exc)

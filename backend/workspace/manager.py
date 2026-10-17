from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import pandas as pd

if TYPE_CHECKING:
    from helpers.storage import StorageManager

logger = logging.getLogger(__name__)

# This file: backend/workspace/manager.py
_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Canonical dataset seeded into every file-based session workspace.
# ``data.parquet`` is the lossless source of truth (preserves dtypes). CSV is an
# export format, not a stored twin. Sessions that carry a real upload name seed
# ``<stem>.parquet`` and track it via ``state.metadata["active_dataset"]``.
CANONICAL_DATA_FILE = "data.parquet"
CANONICAL_CSV_FILE = "data.csv"  # leftover name for resolve_active_dataset back-compat
DEFAULT_DATASET_STEM = "data"

# Stable default when nothing is configured. Resolving to a monorepo-relative path
# (never system temp) guarantees the upload route, the file browser, and every
# subagent agree on one physical workspace directory regardless of whether
# get_config() has loaded yet in the calling context.
DEFAULT_WORKSPACE_REL = "data/workspaces"

# File names the agent writes for its own bookkeeping; never surfaced as artifacts.
_INTERNAL_FILES = frozenset({"sandbox_exec.py", "run_artifact.json", "filter_input.parquet"})

# Directories holding filter-sandbox scratch/intermediate output; contents (whatever
# their filename) are never surfaced as artifacts. See filters/annotate_pandas.py.
_INTERNAL_DIRS = frozenset({"filter_scratch", "filter_annotations", "duckdb_tmp"})

# Memoised base dir so all WorkspaceManager() instantiations across request/agent
# contexts resolve to the identical directory (the temp-vs-data/workspaces split bug).
_RESOLVED_BASE_DIR: Optional[Path] = None


def dataset_stem(filename: Optional[str]) -> str:
    """Filesystem-safe stem derived from an upload/display filename.

    ``titanic.csv`` → ``titanic``; ``Q3 Sales (final).xlsx`` → ``Q3_Sales_final``.
    Falls back to ``data`` when empty so the canonical default is preserved.
    """
    if not filename:
        return DEFAULT_DATASET_STEM
    stem = Path(str(filename)).stem.strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return safe or DEFAULT_DATASET_STEM


def dataset_parquet_name(filename: Optional[str] = None) -> str:
    """On-disk (and UI) name for a seeded dataset: ``{stem}.parquet``."""
    return f"{dataset_stem(filename)}.parquet"


def _monorepo_root() -> Optional[Path]:
    """Parent of backend/ when running from a full stych checkout; else None."""
    parent = _BACKEND_ROOT.parent
    if (parent / "frontend").is_dir() or (
        (parent / "backend").is_dir() and _BACKEND_ROOT.name == "backend"
    ):
        return parent
    return None


def _resolve_workspace_base_dir(
    base_dir_override: Optional[str],
    cfg_base_dir: Optional[str],
) -> Path:
    """
    Resolve the workspace root.

    Precedence: explicit override > WORKSPACE_BASE env (CLEANING_WORKSPACE_BASE
    fallback) > config base_dir > system temp (if nothing configured).

    Relative config paths are anchored at the monorepo root when present
    (…/stych/data/…), so agent writes do not live under backend/ (avoids uvicorn
    --reload). In a backend-only tree (e.g. Docker image at /app), relative paths
    resolve under /data/... on the filesystem.
    """
    raw = (
        base_dir_override
        or os.environ.get("WORKSPACE_BASE")
        or os.environ.get("CLEANING_WORKSPACE_BASE")  # back-compat
        or (cfg_base_dir or "").strip()
        # Never fall back to system temp: a temp dir diverges from where the upload
        # route and the file browser look, so agent-written files become invisible.
        # Anchor on a stable monorepo-relative default instead.
        or DEFAULT_WORKSPACE_REL
    )

    p = Path(raw)
    if p.is_absolute():
        return p.resolve()

    root = _monorepo_root()
    if root is not None:
        return (root / p).resolve()
    # Backend-only layout: e.g. Docker WORKDIR /app, no sibling frontend
    return (Path("/") / p).resolve()


def seed_workspace_data(
    workspace_root: Path,
    df: pd.DataFrame,
    filename: Optional[str] = None,
) -> Path:
    """
    Write the active dataset as ``<stem>.parquet`` into the workspace.

    ``filename`` is the user-facing upload/display name; its stem becomes the on-disk
    name so the agent and the user see one identity for the dataset (``titanic.parquet``
    rather than a generic ``data.parquet`` alias). When omitted, the canonical default
    ``data.parquet`` is used (back-compat).

    Parquet preserves dtypes losslessly so the schema agents see matches what their
    scripts load. CSV is generated on download, not stored as a twin. Returns the
    parquet path.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)
    parquet_path = workspace_root / dataset_parquet_name(filename)
    df.to_parquet(parquet_path, index=False, engine="pyarrow")
    return parquet_path


def seed_all_datasets(workspace_root: Path, state: Any) -> None:
    """Seed *every* file-based dataset in the session (primary + additional) into the
    workspace as ``<stem>.parquet``.

    This is what demotes ``active_dataset`` from a physical switch to a soft default:
    once all datasets live on disk, a sub-agent can ``list_files``/``read_file`` across
    any of them in a single call, and ``active_dataset`` only marks which one is the
    default working frame. SQL sources (no on-disk frame) and datasets already present
    are skipped, so prior cleaning is never clobbered.
    """
    contexts = []
    dc = getattr(state, "data_context", None)
    if dc is not None:
        contexts.append(dc)
    contexts.extend(getattr(state, "additional_data_contexts", None) or [])
    for ctx in contexts:
        try:
            if (
                ctx is None
                or ctx.is_sql_datasource()
                or (callable(getattr(ctx, "is_remote_source", None)) and ctx.is_remote_source())
            ):
                continue
            stem = dataset_stem(ctx.filename)
            if (workspace_root / f"{stem}.parquet").is_file():
                continue
            seed_workspace_data(workspace_root, ctx.working_dataframe, ctx.filename)
        except Exception as exc:  # noqa: BLE001 — per-dataset seeding is best-effort
            logger.warning(
                "[workspace] seed_all_datasets: failed to seed %s: %s",
                getattr(ctx, "filename", "?"),
                exc,
            )


def resolve_active_dataset(
    workspace_root: Path,
    state: Optional[Any] = None,
) -> Optional[tuple[Path, Path, str]]:
    """Resolve the workspace's active dataset as ``(parquet_path, csv_path, stem)``.

    Resolution order, most authoritative first:
      1. an explicit ``state.metadata["active_dataset"]`` pointer (a stem or filename),
      2. the legacy canonical ``data.parquet``/``data.csv`` if present,
      3. a single ``*.parquet`` (or, failing that, ``*.csv``) artifact in the workspace.

    Returns the paths even when the files do not yet exist for case (1), so callers can
    seed/write them. Returns ``None`` only when no candidate can be determined.
    """
    pointer = None
    if state is not None:
        try:
            pointer = (state.metadata or {}).get("active_dataset")
        except Exception:  # noqa: BLE001
            pointer = None

    if pointer:
        stem = dataset_stem(pointer)
        return (workspace_root / f"{stem}.parquet", workspace_root / f"{stem}.csv", stem)

    legacy = workspace_root / CANONICAL_DATA_FILE
    if legacy.is_file():
        return (legacy, workspace_root / CANONICAL_CSV_FILE, DEFAULT_DATASET_STEM)

    parquets = sorted(p for p in workspace_root.glob("*.parquet") if p.is_file())
    if parquets:
        p = parquets[0]
        return (p, p.with_suffix(".csv"), p.stem)
    csvs = sorted(
        c for c in workspace_root.glob("*.csv") if c.is_file() and c.name not in _INTERNAL_FILES
    )
    if csvs:
        c = csvs[0]
        return (c.with_suffix(".parquet"), c, c.stem)
    return None


def list_workspace_artifacts(workspace_root: Path) -> list[dict]:
    """Enumerate every user-visible file in the workspace with light metadata.

    This is the single source of truth for the file browser: it reflects whatever is
    physically present (uploads, retrieved datasets, agent-written outputs) rather than
    a hardcoded filename set. Internal scratch files are hidden.
    """
    if not workspace_root.is_dir():
        return []
    artifacts: list[dict] = []
    for path in sorted(workspace_root.rglob("*")):
        if not path.is_file() or _is_internal_path(path, workspace_root):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        artifacts.append(
            {
                "name": path.name,
                "path": str(path.relative_to(workspace_root)),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "suffix": path.suffix.lower().lstrip("."),
            }
        )
    return artifacts


def _is_internal_path(path: Path, workspace_root: Path) -> bool:
    if path.name in _INTERNAL_FILES:
        return True
    try:
        rel_parts = path.relative_to(workspace_root).parts
    except ValueError:
        return True
    return bool(_INTERNAL_DIRS.intersection(rel_parts[:-1]))


class WorkspaceManager:
    """
    Manages a per-session scratch workspace shared by all ReAct subagents.
    Files written here are sandboxed to the workspace root.
    """

    def __init__(self, base_dir: Optional[str] = None):
        global _RESOLVED_BASE_DIR
        # Reuse the first resolved base dir for all default instantiations so the
        # upload route and the agents never diverge to different physical roots.
        if base_dir is None and _RESOLVED_BASE_DIR is not None:
            self.base_dir = _RESOLVED_BASE_DIR
            self.base_dir.mkdir(parents=True, exist_ok=True)
            return
        cfg = {}
        try:
            # Deferred: api.dependencies pulls in the agent tree, which imports
            # back into this module, so a module-level import closes a cycle.
            from api.dependencies import get_config  # noqa: PLC0415

            full = get_config()
            # New key "workspace"; fall back to legacy "cleaning_workspace".
            cfg = full.get("workspace", {}) or full.get("cleaning_workspace", {})
        except Exception:
            cfg = {}
        cfg_base = cfg.get("base_dir")
        self.base_dir = _resolve_workspace_base_dir(base_dir, cfg_base)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if base_dir is None:
            _RESOLVED_BASE_DIR = self.base_dir

    def get_or_create(self, session_id: str, user_id: Optional[str] = None) -> Path:
        safe_user = (user_id or "anonymous").replace("/", "_").replace("\\", "_")
        safe_session = session_id.replace("/", "_").replace("\\", "_")
        root = (self.base_dir / safe_user / safe_session).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def cleanup(self, session_id: str, user_id: Optional[str] = None) -> None:
        safe_user = (user_id or "anonymous").replace("/", "_").replace("\\", "_")
        safe_session = session_id.replace("/", "_").replace("\\", "_")
        root = self.base_dir / safe_user / safe_session
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    async def archive_to_storage(
        self,
        session_id: str,
        user_id: Optional[str],
        storage: "StorageManager",
    ) -> int:
        """
        Upload all files in the local workspace to storage.
        Runs file I/O in a thread pool so it doesn't block the event loop.
        Returns count of files archived.
        """
        effective_user = user_id or "anonymous"
        workspace = self.get_or_create(session_id, user_id=user_id)
        files = [
            f for f in workspace.rglob("*") if f.is_file() and not _is_internal_path(f, workspace)
        ]
        if not files:
            return 0

        def _upload() -> int:
            count = 0
            for f in files:
                try:
                    storage.save_workspace_file(effective_user, session_id, f.name, f.read_bytes())
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "[workspace] archive failed for %s/%s/%s: %s",
                        effective_user,
                        session_id,
                        f.name,
                        exc,
                    )
            return count

        count = await asyncio.to_thread(_upload)
        logger.info(
            "[workspace] archived %d file(s) for %s/%s",
            count,
            effective_user,
            session_id,
        )
        return count

    def restore_from_storage(
        self,
        session_id: str,
        user_id: Optional[str],
        storage: "StorageManager",
    ) -> int:
        """
        Download archived workspace files from storage for files not already
        present locally.  Returns count of files restored.
        """
        effective_user = user_id or "anonymous"
        workspace = self.get_or_create(session_id, user_id=user_id)
        archived = storage.list_workspace_files(effective_user, session_id)
        if not archived:
            return 0

        count = 0
        for name in archived:
            dest = workspace / name
            if dest.exists():
                continue
            content = storage.load_workspace_file(effective_user, session_id, name)
            if content:
                try:
                    dest.write_bytes(content)
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "[workspace] restore failed for %s/%s/%s: %s",
                        effective_user,
                        session_id,
                        name,
                        exc,
                    )

        if count:
            logger.info(
                "[workspace] restored %d file(s) for %s/%s",
                count,
                effective_user,
                session_id,
            )
        return count


# Back-compat alias during migration.
CleaningWorkspaceManager = WorkspaceManager

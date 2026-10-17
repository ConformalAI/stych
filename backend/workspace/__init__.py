"""Shared per-session workspace for all ReAct subagents.

Provides:
- ``WorkspaceManager`` — resolves and creates the per-session workspace directory.
- ``WorkspaceSandboxRunner`` — runs arbitrary Python in that workspace with guardrails.
- ``CANONICAL_DATA_FILE`` / ``seed_workspace_data`` — the canonical ``data.parquet``
  that agents read and write.
"""

from __future__ import annotations

from workspace.manager import (
    CANONICAL_CSV_FILE,
    CANONICAL_DATA_FILE,
    DEFAULT_DATASET_STEM,
    WorkspaceManager,
    dataset_parquet_name,
    dataset_stem,
    list_workspace_artifacts,
    resolve_active_dataset,
    seed_workspace_data,
)
from workspace.runner import WorkspaceSandboxRunner

# Back-compat alias during migration — old imports still resolve.
CleaningWorkspaceManager = WorkspaceManager

__all__ = [
    "WorkspaceManager",
    "CleaningWorkspaceManager",
    "WorkspaceSandboxRunner",
    "seed_workspace_data",
    "resolve_active_dataset",
    "list_workspace_artifacts",
    "dataset_stem",
    "dataset_parquet_name",
    "CANONICAL_DATA_FILE",
    "CANONICAL_CSV_FILE",
    "DEFAULT_DATASET_STEM",
]

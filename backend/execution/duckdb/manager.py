"""Lazy lifecycle management for isolated session DuckDB kernels."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .kernel import DuckDBKernel
from .types import DuckDBKernelConfig, DuckDBKernelLocation

_TEMP_DIR = "duckdb_tmp"
logger = logging.getLogger(__name__)


class DuckDBKernelManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kernels: dict[tuple[str, str], DuckDBKernel] = {}

    @staticmethod
    def _key(session_id: str, user_id: str | None) -> tuple[str, str]:
        return (str(user_id or "anonymous"), session_id)

    def get_or_create(
        self,
        session_id: str,
        user_id: str | None = None,
        *,
        workspace_root: Path | None = None,
        config: DuckDBKernelConfig | None = None,
    ) -> DuckDBKernel:
        key = self._key(session_id, user_id)
        with self._lock:
            existing = self._kernels.get(key)
            if existing is not None and not existing.closed:
                return existing
            if workspace_root is None:
                from workspace.manager import WorkspaceManager  # noqa: PLC0415

                root = WorkspaceManager().get_or_create(session_id, user_id=user_id)
            else:
                root = workspace_root
            kernel = DuckDBKernel(session_id, Path(root) / _TEMP_DIR, config=config)
            self._kernels[key] = kernel
            return kernel

    def location(self, session_id: str, user_id: str | None = None) -> DuckDBKernelLocation | None:
        key = self._key(session_id, user_id)
        with self._lock:
            kernel = self._kernels.get(key)
            if kernel is None or kernel.closed:
                return None
            return DuckDBKernelLocation(
                session_id=session_id,
                user_id=key[0],
                temp_directory=kernel.temp_directory,
            )

    def close_session(self, session_id: str, user_id: str | None = None) -> int:
        with self._lock:
            if user_id is None:
                keys = [key for key in self._kernels if key[1] == session_id]
            else:
                key = self._key(session_id, user_id)
                keys = [key] if key in self._kernels else []
            kernels = [self._kernels.pop(key) for key in keys]
        for kernel in kernels:
            try:
                kernel.close()
            except Exception as exc:
                logger.warning("Failed to close DuckDB kernel for session %s: %s", session_id, exc)
        return len(kernels)

    def close_all(self) -> int:
        with self._lock:
            kernels = list(self._kernels.values())
            self._kernels.clear()
        for kernel in kernels:
            try:
                kernel.close()
            except Exception as exc:
                logger.warning(
                    "Failed to close DuckDB kernel for session %s: %s",
                    kernel.session_id,
                    exc,
                )
        return len(kernels)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(not kernel.closed for kernel in self._kernels.values())


duckdb_kernel_manager = DuckDBKernelManager()

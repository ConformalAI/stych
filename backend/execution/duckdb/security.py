"""Host-filesystem authorization for Arrow-backed DuckDB inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pyarrow.dataset as pads

from .types import DuckDBUnauthorizedPath


class DuckDBPathPolicy:
    """Restrict file-backed Arrow inputs to resolved tenant workspace roots."""

    def __init__(self, roots: Iterable[Path]) -> None:
        self.roots = tuple(Path(root).resolve() for root in roots)
        if not self.roots:
            raise ValueError("At least one authorized data root is required")

    def authorize_file(self, path: Path) -> Path:
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DuckDBUnauthorizedPath("The requested data file is unavailable") from exc
        if not resolved.is_file() or not any(_is_within(resolved, root) for root in self.roots):
            raise DuckDBUnauthorizedPath(
                "The requested data file is outside this session's authorized workspace"
            )
        return resolved

    def authorize_dataset(self, dataset: pads.Dataset) -> None:
        if isinstance(dataset, pads.InMemoryDataset):
            return
        if isinstance(dataset, pads.FileSystemDataset):
            for path in dataset.files:
                self.authorize_file(Path(path))
            return
        raise DuckDBUnauthorizedPath(
            "Only in-memory datasets or file datasets inside this session's workspace are allowed"
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True

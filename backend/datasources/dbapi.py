"""Shared DB-API session helpers for warehouse connectors.

Drivers still own connect/execute details; this only avoids opening a new
session for every statement in one request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Optional, TypeVar

T = TypeVar("T")


@contextmanager
def borrowed(existing: Optional[T], factory: Callable[[], T]) -> Iterator[T]:
    """Use *existing* if given; otherwise open with *factory* and close on exit."""
    owns = existing is None
    handle = factory() if owns else existing
    try:
        yield handle
    finally:
        if owns and handle is not None:
            closer = getattr(handle, "close", None)
            if closer is not None:
                closer()

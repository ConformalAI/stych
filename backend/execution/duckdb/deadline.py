"""Deadline enforcement for serialized DuckDB connection work."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from .types import DuckDBQueryTimeout

_Result = TypeVar("_Result")


def run_with_deadline(
    connection,
    action: Callable[[], _Result],
    timeout_seconds: float,
    *,
    label: str,
) -> _Result:
    done = threading.Event()
    timed_out = threading.Event()

    def watch() -> None:
        if not done.wait(timeout_seconds):
            timed_out.set()
            connection.interrupt()

    watchdog = threading.Thread(target=watch, name="duckdb-query-watchdog", daemon=True)
    watchdog.start()
    try:
        result = action()
    except Exception as exc:
        if timed_out.is_set():
            raise DuckDBQueryTimeout(f"{label} exceeded {timeout_seconds:g} seconds") from exc
        raise
    finally:
        done.set()
        # Joining before the execution lock is released prevents a late interrupt
        # from leaking into the next statement on this connection.
        watchdog.join()
    if timed_out.is_set():
        raise DuckDBQueryTimeout(f"{label} exceeded {timeout_seconds:g} seconds")
    return result

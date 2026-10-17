"""Streaming export of a connected source's full table.

Bytes go source → this process → client socket. Nothing is spooled to disk or to
object storage, so exporting a connected table never leaves connector data with
us. Both formats are produced incrementally: CSV row-by-row, Parquet as row
groups followed by its footer (the format is written forward-only even though it
is *read* by seeking, so no buffering of the whole table is required).

Which path serves a request is decided by the connector manifest, never by a
connector id: a source whose ``export_snapshot`` resolves to full scope streams
from its ``TableAdapter``; anything else falls back to the in-memory frame.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

import pandas as pd

from datasources import adapters as table_adapters
from datasources.adapters.base import EXPORT_BATCH_ROWS
from datasources.contracts import Capability, DataScope, SupportMode
from datasources.registry import get_manifest

logger = logging.getLogger(__name__)

# Ceiling on a single export. Large enough to be useful, low enough that one
# request cannot stream a warehouse table for hours. Override per deployment.
DEFAULT_EXPORT_MAX_ROWS = 5_000_000

FORMAT_MEDIA_TYPES = {
    "csv": "text/csv",
    "parquet": "application/octet-stream",
}


class ExportError(Exception):
    """Export cannot be served as requested."""


def export_max_rows(config: Optional[dict[str, Any]] = None) -> int:
    """Row cap: ``EXPORT_MAX_ROWS`` env, else config, else the default."""
    raw = os.getenv("EXPORT_MAX_ROWS")
    if raw is None and config:
        raw = (config.get("datasources", {}).get("export", {}) or {}).get("max_rows")
    if raw is None:
        return DEFAULT_EXPORT_MAX_ROWS
    try:
        value = int(str(raw).replace("_", ""))
    except (TypeError, ValueError):
        logger.warning("Invalid export row cap %r; using default.", raw)
        return DEFAULT_EXPORT_MAX_ROWS
    return value if value > 0 else DEFAULT_EXPORT_MAX_ROWS


@dataclass(frozen=True)
class ExportPlan:
    """How one source's export will be served, and at what scope."""

    streamed: bool
    scope: DataScope
    row_cap: int
    reason: str


def plan_export(ctx: Any, config: Optional[dict[str, Any]] = None) -> ExportPlan:
    """Decide between the live source stream and the loaded frame."""
    cap = export_max_rows(config)
    meta = getattr(ctx, "metadata", None) or {}
    source = str(meta.get("source") or "upload")
    try:
        support = get_manifest(source).capabilities[Capability.EXPORT_SNAPSHOT]
    except KeyError:
        return ExportPlan(False, DataScope.SAMPLE, cap, "unknown_connector")
    full = support.mode != SupportMode.UNSUPPORTED and support.scope == DataScope.FULL
    if not full or source == "upload":
        return ExportPlan(False, support.scope or DataScope.SAMPLE, cap, "frame_export")
    if not table_adapters.supports_pushdown(meta):
        # Declared full, but this session's credentials are gone. Say so rather
        # than silently handing back the stale connect-time sample.
        raise ExportError(
            "The connection for this source is unavailable — reconnect the table to export it."
        )
    return ExportPlan(True, DataScope.FULL, cap, "source_stream")


def iter_source_batches(
    ctx: Any,
    variant: str,
    *,
    row_cap: int,
    batch_rows: int = EXPORT_BATCH_ROWS,
) -> Iterator[pd.DataFrame]:
    """Batches of the live relation, stopping at *row_cap*."""
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415 — avoid a cycle

    adapter = table_adapters.resolve_adapter(ctx, variant)
    # A downloaded file must contain the user's columns, never Stych's row stamp.
    columns = wc.visible_columns(c.name for c in adapter.schema())
    emitted = 0
    for batch in adapter.stream_batches(columns=columns, batch_rows=batch_rows, max_rows=row_cap):
        if batch.empty:
            continue
        remaining = row_cap - emitted
        if remaining <= 0:
            return
        if len(batch) > remaining:
            batch = batch.iloc[:remaining]
        emitted += len(batch)
        yield batch
        if emitted >= row_cap:
            return


def iter_frame_batches(frame: pd.DataFrame, *, row_cap: int) -> Iterator[pd.DataFrame]:
    """The loaded frame as one capped batch, so both paths share a writer."""
    yield frame.iloc[:row_cap] if len(frame) > row_cap else frame


def csv_chunks(batches: Iterable[pd.DataFrame]) -> Iterator[bytes]:
    """Encode batches as one CSV document; the header is written once."""
    first = True
    empty = True
    for batch in batches:
        empty = False
        yield batch.to_csv(index=False, header=first).encode("utf-8")
        first = False
    if empty:
        yield b""


class _ForwardOnlySink(io.RawIOBase):
    """A writable that hands each write straight to the response stream.

    Parquet needs ``tell()`` to record row-group offsets but never seeks while
    writing, so tracking a running position is enough to write the file forward.
    """

    def __init__(self) -> None:
        self._pending: list[bytes] = []
        self._position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data) -> int:  # type: ignore[override]
        payload = bytes(data)
        self._pending.append(payload)
        self._position += len(payload)
        return len(payload)

    def tell(self) -> int:
        return self._position

    def drain(self) -> bytes:
        if not self._pending:
            return b""
        out = b"".join(self._pending)
        self._pending.clear()
        return out


def parquet_chunks(batches: Iterable[pd.DataFrame]) -> Iterator[bytes]:
    """Encode batches as one Parquet file, emitting each row group as written."""
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    sink = _ForwardOnlySink()
    writer: Optional[pq.ParquetWriter] = None
    try:
        for batch in batches:
            table = pa.Table.from_pandas(batch, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(sink, table.schema)
            else:
                # Later batches must match the first batch's schema; a column
                # that arrived all-null in batch 1 can otherwise infer differently.
                table = table.cast(writer.schema)
            writer.write_table(table)
            chunk = sink.drain()
            if chunk:
                yield chunk
        if writer is None:
            writer = pq.ParquetWriter(sink, pa.schema([]))
    finally:
        if writer is not None:
            writer.close()
    tail = sink.drain()
    if tail:
        yield tail


def encode(batches: Iterable[pd.DataFrame], fmt: str) -> Iterator[bytes]:
    if fmt == "csv":
        return csv_chunks(batches)
    if fmt == "parquet":
        return parquet_chunks(batches)
    raise ExportError(f"Unsupported format '{fmt}'. Supported: csv, parquet.")

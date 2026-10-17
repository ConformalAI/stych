"""Metadata-only attachment for live database sources."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

import pandas as pd

from agents.state import DataContext, state_manager
from api.dataset_attach import reject_if_already_attached
from datasources import adapters as table_adapters
from llm.langfuse_client import get_langfuse_tracker

logger = logging.getLogger(__name__)


def _empty_frame(columns: list[table_adapters.AdapterColumn]) -> pd.DataFrame:
    return pd.DataFrame(columns=[column.name for column in columns])


def _column_metadata(columns: list[table_adapters.AdapterColumn]) -> list[dict[str, str]]:
    return [
        {"name": column.name, "db_type": column.db_type, "kind": column.kind} for column in columns
    ]


def _summary(columns: list[table_adapters.AdapterColumn], rows: int) -> dict[str, Any]:
    names = [column.name for column in columns]
    return {
        "rows": rows,
        "columns": len(columns),
        "column_names": names,
        "dtypes": {column.name: column.db_type for column in columns},
        "source_native": True,
    }


def _probe_source(context: DataContext) -> tuple[list[table_adapters.AdapterColumn], int]:
    adapter = table_adapters.resolve_adapter(context)
    with adapter.bound_session():
        columns = adapter.schema()
        rows = adapter.count().value
    return columns, rows


async def attach_remote_source(
    *,
    connector_id: str,
    connector_label: str,
    source_label: str,
    schema: str | None,
    table: str | None,
    custom_query: str | None,
    session_id: str | None,
    user: str,
    connection_id: str | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Attach a live source without ingesting or persisting a row sample."""
    resolved_session_id = session_id or str(uuid.uuid4())
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        session = state_manager.get_or_create_session(resolved_session_id, app_user_id=user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Session belongs to another user.") from exc
    incoming = {
        "source": connector_id,
        "schema": schema,
        "table": table,
        "custom_query": custom_query,
        "database": metadata.get("database"),
        "connection_id": connection_id,
    }
    reject_if_already_attached(session, incoming, f"{connector_label}: {source_label}")

    source_id = f"{connector_id}:{schema or 'default'}.{table or 'query'}:{uuid.uuid4().hex[:8]}"
    context_metadata = {
        **metadata,
        "user": user,
        "source": connector_id,
        "source_label": source_label,
        "schema": schema,
        "table": table,
        "custom_query": custom_query,
        "connection_id": connection_id,
        "storage_mode": "remote_reference",
        "source_row_count": 0,
        "source_columns": [],
    }
    empty = pd.DataFrame()
    context = DataContext(
        file_id=source_id,
        filename=f"{connector_label}: {source_label}",
        raw_dataframe=empty,
        working_dataframe=empty,
        uploaded_at=datetime.now(),
        metadata=context_metadata,
    )
    try:
        columns, rows = await asyncio.to_thread(_probe_source, context)
    except table_adapters.AdapterError as exc:
        raise ValueError(str(exc)) from exc

    context.metadata["source_columns"] = _column_metadata(columns)
    context.metadata["source_row_count"] = rows
    # Source adapters obtain their authoritative schema remotely.  The empty,
    # shared frame only preserves DataContext's existing non-null invariant.
    context.raw_dataframe = _empty_frame(columns)
    context.working_dataframe = context.raw_dataframe
    session.add_data_context(context)
    state_manager.mark_dirty(resolved_session_id)
    await state_manager.snapshot_data_async(session)

    tracker = get_langfuse_tracker()
    if tracker:
        tracker.set_session_id(resolved_session_id)
        tracker.set_user_id(user)
    logger.info(
        "Attached live %s source %s to session %s for user %s",
        connector_label,
        source_label,
        resolved_session_id,
        user,
    )

    return {
        "session_id": resolved_session_id,
        "source_id": source_id,
        "source_label": source_label,
        "source_type": connector_id,
        "rows": rows,
        "columns": len(columns),
        "column_names": [column.name for column in columns],
        "summary": _summary(columns, rows),
        "message": (
            f"Connected to {connector_label}: {source_label}. "
            "Data remains in the source; previews, analysis, and filters run live."
        ),
    }

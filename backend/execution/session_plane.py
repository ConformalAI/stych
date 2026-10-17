"""Resolve session datasets onto their declared analytical execution plane."""

from __future__ import annotations

import hashlib
import json
import threading
import weakref
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import pandas as pd
import pyarrow as pa

from datasources.contracts import Capability, DataFreshness, DataScope, SupportMode
from datasources.contracts.joins import CROSS_SOURCE_UNAVAILABLE_MESSAGE
from datasources.registry import SOURCE_WORKING_COPY_IDS, get_manifest
from execution.duckdb import DuckDBKernel, DuckDBRelation, duckdb_kernel_manager
from execution.duckdb.naming import source_relation_name
from workspace.manager import WorkspaceManager

_BINDING_LOCK_GUARD = threading.Lock()
_BINDING_LOCKS: weakref.WeakKeyDictionary[DuckDBKernel, dict[str, threading.RLock]] = (
    weakref.WeakKeyDictionary()
)


class PlaneKind(str, Enum):
    SOURCE = "source"
    DUCKDB = "duckdb"


@dataclass(frozen=True)
class ResolvedDataPlane:
    kind: PlaneKind
    connector_id: str
    source_id: str
    scope: DataScope
    freshness: DataFreshness
    row_count: int
    row_limit: Optional[int]
    truncated: bool
    schema_complete: bool
    snapshot_token: str
    kernel: Optional[DuckDBKernel] = None
    relation: Optional[DuckDBRelation] = None

    @property
    def relation_name(self) -> str:
        if self.relation is None:
            raise ValueError("Source-native execution planes do not have a DuckDB relation")
        return self.relation.name


def _connector_id(dc: Any) -> str:
    return str((dc.metadata or {}).get("source") or "upload")


def _snapshot_token(dc: Any, connector_id: str) -> str:
    frame = dc.working_dataframe
    generation = int((dc.metadata or {}).get("data_generation") or 0)
    schema = "|".join(f"{column}:{frame[column].dtype}" for column in frame.columns)
    try:
        values = pd.util.hash_pandas_object(frame, index=True, categorize=True).values.tobytes()
    except (TypeError, ValueError):
        # Mixed document columns can contain unhashable lists/dicts. Canonical
        # JSON text keeps invalidation correct without losing null distinctions.
        canonical = frame.copy(deep=False)
        for column in canonical.columns:
            if canonical[column].dtype == "object":
                canonical[column] = canonical[column].map(_canonical_object)
        values = pd.util.hash_pandas_object(canonical, index=True, categorize=True).values.tobytes()
    digest = hashlib.sha256()
    digest.update(f"phase3:{connector_id}:{dc.file_id}:{generation}:{len(frame)}:{schema}".encode())
    digest.update(values)
    return digest.hexdigest()


def _canonical_object(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
    try:
        hash(value)
        return value
    except TypeError:
        return str(value)


def _arrow_snapshot(frame: pd.DataFrame) -> tuple[pa.Table, list[str]]:
    """Convert a frame to Arrow, normalizing only columns Arrow rejects."""
    try:
        return pa.Table.from_pandas(frame, preserve_index=False), []
    except (pa.ArrowException, TypeError, ValueError):
        normalized = frame.copy(deep=False)
        normalized_columns: list[str] = []
        for column in normalized.columns:
            try:
                pa.array(normalized[column], from_pandas=True)
            except (pa.ArrowException, TypeError, ValueError):
                normalized = normalized.copy() if not normalized_columns else normalized
                normalized[column] = normalized[column].map(_canonical_object)
                normalized_columns.append(str(column))
        return pa.Table.from_pandas(normalized, preserve_index=False), normalized_columns


def _binding_lock(kernel: DuckDBKernel, relation_name: str) -> threading.RLock:
    with _BINDING_LOCK_GUARD:
        locks = _BINDING_LOCKS.setdefault(kernel, {})
        return locks.setdefault(relation_name, threading.RLock())


def _scope_details(dc: Any, connector_id: str) -> tuple[DataScope, Optional[int], bool]:
    manifest = get_manifest(connector_id)
    support = manifest.capabilities[Capability.FULL_SCAN]
    metadata = dc.metadata or {}
    limit = metadata.get("snapshot_row_limit", support.limits.get("rows"))
    row_limit = int(limit) if isinstance(limit, int) else None
    scope = support.scope or DataScope.SAMPLE
    truncated = bool(
        metadata.get(
            "snapshot_truncated",
            row_limit is not None and len(dc.working_dataframe) >= row_limit,
        )
    )
    return scope, row_limit, truncated


def _schema_complete(connector_id: str, dc: Any = None) -> bool:
    metadata = (dc.metadata or {}) if dc is not None else {}
    if "snapshot_schema_complete" in metadata:
        return bool(metadata["snapshot_schema_complete"])
    support = get_manifest(connector_id).capabilities[Capability.SCHEMA_COMPLETE]
    return support.mode != SupportMode.UNSUPPORTED


def uses_source_working_copy(dc: Any) -> bool:
    """True when analysis/cleaning should clone into a source-owned scratch schema.

    Postgres uses ``stych_temp`` in the customer's database. Databricks uses a
    Unity Catalog scratch schema (default ``{catalog}.stych_temp``). Both need
    live credentials; a restored snapshot without them falls back to DuckDB.
    """
    connector_id = _connector_id(dc)
    if connector_id not in SOURCE_WORKING_COPY_IDS:
        return False
    return bool(callable(getattr(dc, "is_sql_datasource", None)) and dc.is_sql_datasource())


def resolve_data_plane(
    state: Any,
    data_context: Any = None,
    *,
    snapshot_postgres: bool = False,
    force: bool = False,
) -> ResolvedDataPlane:
    """Return the source-native plane (Postgres/Databricks) or a session DuckDB snapshot."""
    dc = data_context or getattr(state, "data_context", None)
    if dc is None:
        raise ValueError("No dataset loaded — upload a file or connect a database first.")
    connector_id = _connector_id(dc)
    get_manifest(connector_id)  # Unknown connectors fail closed.

    live_database = connector_id in SOURCE_WORKING_COPY_IDS or (
        callable(getattr(dc, "is_remote_source", None)) and dc.is_remote_source()
    )
    source_available = uses_source_working_copy(dc)
    if live_database:
        if snapshot_postgres:
            # Warehouse rows are never copied into the session DuckDB kernel.
            raise ValueError(CROSS_SOURCE_UNAVAILABLE_MESSAGE)
        if source_available:
            return ResolvedDataPlane(
                kind=PlaneKind.SOURCE,
                connector_id=connector_id,
                source_id=str(dc.file_id),
                scope=DataScope.FULL,
                freshness=DataFreshness.LIVE,
                row_count=int((dc.metadata or {}).get("source_row_count") or 0),
                row_limit=None,
                truncated=False,
                schema_complete=True,
                snapshot_token="source-native",
            )
        raise ValueError(
            "The live connection for this source is unavailable. Reconnect it before continuing."
        )

    user_id = (getattr(state, "metadata", None) or {}).get("user_id")
    workspace = WorkspaceManager().get_or_create(state.session_id, user_id=user_id)
    kernel = duckdb_kernel_manager.get_or_create(
        state.session_id,
        user_id,
        workspace_root=workspace,
    )
    relation_name = source_relation_name(str(dc.file_id), str(dc.filename))
    token = _snapshot_token(dc, connector_id)
    normalized_columns = list(
        ((dc.metadata or {}).get("analytical_snapshot") or {}).get("normalized_columns") or []
    )
    scope, row_limit, truncated = _scope_details(dc, connector_id)
    with _binding_lock(kernel, relation_name):
        existing = next((item for item in kernel.relations() if item.name == relation_name), None)
        if force or existing is None or existing.source != token:
            table, normalized_columns = _arrow_snapshot(dc.working_dataframe)
            existing = kernel.materialize(
                relation_name,
                table,
                replace=existing is not None,
                source=token,
            )
        # Published under the same lock so the metadata always describes the
        # relation that was just bound, not a concurrent caller's.
        dc.metadata["analytical_snapshot"] = {
            "engine": "duckdb",
            "scope": scope.value,
            "freshness": DataFreshness.SNAPSHOT.value,
            "rows": len(dc.working_dataframe),
            "row_limit": row_limit,
            "truncated": truncated,
            "schema_complete": _schema_complete(connector_id, dc),
            "normalized_columns": normalized_columns,
            "token": token,
        }
    return ResolvedDataPlane(
        kind=PlaneKind.DUCKDB,
        connector_id=connector_id,
        source_id=str(dc.file_id),
        scope=scope,
        freshness=DataFreshness.SNAPSHOT,
        row_count=len(dc.working_dataframe),
        row_limit=row_limit,
        truncated=truncated,
        schema_complete=_schema_complete(connector_id, dc),
        snapshot_token=token,
        kernel=kernel,
        relation=existing,
    )


def plane_to_pandas(plane: ResolvedDataPlane) -> pd.DataFrame:
    if plane.kind != PlaneKind.DUCKDB or plane.kernel is None:
        raise ValueError("Only DuckDB snapshot planes can be converted to pandas")
    result = plane.kernel.query(
        f'SELECT * FROM "{plane.relation_name}"',
        max_rows=max(1, plane.row_count),
    )
    return result.to_pandas()

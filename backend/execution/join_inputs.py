"""Plan and load complete join inputs without silently using connector samples.

The connect-time DataFrame for a database source is a bounded planning sample.
It is never a valid execution input.  This module is the single boundary that
turns attached assets into complete DataFrames for Stych-managed linking
strategies (currently the existing lexical/fuzzy matchers and the exact
fallback when native pushdown is unavailable).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import pandas as pd

from datasources import adapters as table_adapters
from datasources.adapters.base import EXPORT_BATCH_ROWS
from datasources.contracts import DataFreshness, DataScope
from datasources.source_identity import source_key

DEFAULT_JOIN_MAX_INPUT_BYTES = 1024**3
DEFAULT_JOIN_MAX_INPUT_ROWS = 5_000_000


class JoinInputError(Exception):
    """A complete join input cannot be resolved within the approved policy."""


def _positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).replace("_", ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class JoinInputPlan:
    name: str
    connector: str
    rows: int
    estimated_bytes: int
    scope: DataScope
    freshness: DataFreshness
    transfer_required: bool
    variant: str
    relation_ref: str
    fingerprint: str


@dataclass(frozen=True)
class FullJoinInputPlan:
    inputs: tuple[JoinInputPlan, ...]
    estimated_transfer_bytes: int
    max_input_bytes: int
    max_input_rows: int

    @property
    def over_budget(self) -> bool:
        return self.estimated_transfer_bytes > self.max_input_bytes or any(
            item.rows > self.max_input_rows for item in self.inputs
        )

    @property
    def connected(self) -> bool:
        return any(item.connector != "upload" for item in self.inputs)


def join_input_plan_payload(plan: FullJoinInputPlan) -> dict[str, Any]:
    """JSON-safe confirmation payload; never includes credentials."""
    return {
        "inputs": [
            {
                "name": item.name,
                "connector": item.connector,
                "rows": item.rows,
                "estimated_bytes": item.estimated_bytes,
                "scope": item.scope.value,
                "freshness": item.freshness.value,
                "transfer_required": item.transfer_required,
                "variant": item.variant,
                "relation_ref": item.relation_ref,
                "fingerprint": item.fingerprint,
            }
            for item in plan.inputs
        ],
        "estimated_transfer_bytes": plan.estimated_transfer_bytes,
        "max_input_bytes": plan.max_input_bytes,
        "max_input_rows": plan.max_input_rows,
        "over_budget": plan.over_budget,
    }


def join_input_plan_from_payload(payload: Mapping[str, Any]) -> FullJoinInputPlan:
    """Rebuild a previously confirmed plan, failing closed on malformed state."""
    try:
        inputs = tuple(
            JoinInputPlan(
                name=str(item["name"]),
                connector=str(item["connector"]),
                rows=int(item["rows"]),
                estimated_bytes=int(item["estimated_bytes"]),
                scope=DataScope(str(item["scope"])),
                freshness=DataFreshness(str(item["freshness"])),
                transfer_required=bool(item["transfer_required"]),
                variant=str(item["variant"]),
                relation_ref=str(item["relation_ref"]),
                fingerprint=str(item["fingerprint"]),
            )
            for item in payload["inputs"]
        )
        return FullJoinInputPlan(
            inputs=inputs,
            estimated_transfer_bytes=int(payload["estimated_transfer_bytes"]),
            max_input_bytes=int(payload["max_input_bytes"]),
            max_input_rows=int(payload["max_input_rows"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise JoinInputError("The confirmed full-data input plan is invalid.") from exc


def _frame_bytes_per_row(frame: pd.DataFrame) -> int:
    if frame.empty:
        return max(8, len(frame.columns) * 8)
    total = int(frame.memory_usage(index=False, deep=True).sum())
    return max(1, (total + len(frame) - 1) // len(frame))


def _variant(ctx: Any) -> str:
    meta = getattr(ctx, "metadata", None) or {}
    return "temp" if meta.get("temp_schema") and meta.get("temp_table") else "base"


def _relation_ref(ctx: Any, variant: str) -> str:
    meta = getattr(ctx, "metadata", None) or {}
    if variant == "temp":
        return f"{meta.get('temp_schema')}.{meta.get('temp_table')}"
    if meta.get("source") == "mongodb":
        return f"{meta.get('database')}.{meta.get('collection')}"
    if meta.get("custom_query"):
        digest = hashlib.sha256(str(meta["custom_query"]).encode()).hexdigest()[:16]
        return f"query:{digest}"
    return f"{meta.get('schema')}.{meta.get('table')}"


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


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    schema = "|".join(f"{column}:{frame[column].dtype}" for column in frame.columns)
    try:
        values = pd.util.hash_pandas_object(frame, index=True, categorize=True).values.tobytes()
    except (TypeError, ValueError):
        canonical = frame.copy(deep=False)
        for column in canonical.columns:
            if canonical[column].dtype == "object":
                canonical[column] = canonical[column].map(_canonical_object)
        values = pd.util.hash_pandas_object(canonical, index=True, categorize=True).values.tobytes()
    digest = hashlib.sha256()
    digest.update(f"{len(frame)}:{schema}".encode())
    digest.update(values)
    return digest.hexdigest()


def join_context_fingerprint(ctx: Any) -> str:
    """Credential-free identity of the exact logical input being confirmed."""
    meta = getattr(ctx, "metadata", None) or {}
    connector = str(meta.get("source") or "upload")
    variant = _variant(ctx)
    relation = _relation_ref(ctx, variant)
    file_id = str(getattr(ctx, "file_id", "") or "")
    generation = int(meta.get("data_generation") or 0)
    frame = getattr(ctx, "working_dataframe", None)
    if frame is None:
        frame = getattr(ctx, "dataframe", None)
    if frame is None:
        raise JoinInputError(f"Dataset '{getattr(ctx, 'filename', 'unknown')}' has no data.")
    if connector == "upload":
        identity = _frame_fingerprint(frame)
    else:
        identity = source_key(meta)
        if identity is None:
            raise JoinInputError(
                f"The source identity for '{getattr(ctx, 'filename', 'unknown')}' is unavailable. "
                "Reconnect it before running a join."
            )
    raw = f"join-input-v1:{connector}:{identity}:{file_id}:{generation}:{variant}:{relation}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _iter_source_batches(ctx: Any, variant: str, *, expected_rows: int):
    """Stream one row beyond the plan so growth cannot be silently truncated."""
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415

    adapter = table_adapters.resolve_adapter(ctx, variant)
    connector = str((getattr(ctx, "metadata", None) or {}).get("source") or "upload")
    columns = None
    if connector != "mongodb":
        columns = wc.visible_columns(column.name for column in adapter.schema())
    yield from adapter.stream_batches(
        columns=columns,
        batch_rows=EXPORT_BATCH_ROWS,
        max_rows=expected_rows + 1,
    )


def plan_full_join_inputs(
    contexts: Iterable[Any],
    *,
    max_input_bytes: int | None = None,
    max_input_rows: int | None = None,
) -> FullJoinInputPlan:
    """Inspect complete input sizes before a user confirms a materialized join."""
    byte_limit = max_input_bytes or _positive_env_int(
        "JOIN_MAX_INPUT_BYTES", DEFAULT_JOIN_MAX_INPUT_BYTES
    )
    row_limit = max_input_rows or _positive_env_int(
        "JOIN_MAX_INPUT_ROWS", DEFAULT_JOIN_MAX_INPUT_ROWS
    )
    planned: list[JoinInputPlan] = []
    transferred = 0
    for ctx in contexts:
        meta = getattr(ctx, "metadata", None) or {}
        connector = str(meta.get("source") or "upload")
        frame = getattr(ctx, "working_dataframe", None)
        if frame is None:
            frame = getattr(ctx, "dataframe", None)
        if frame is None:
            raise JoinInputError(f"Dataset '{getattr(ctx, 'filename', 'unknown')}' has no data.")
        variant = _variant(ctx)
        if connector == "upload":
            rows = len(frame)
            estimated = int(frame.memory_usage(index=False, deep=True).sum())
            item = JoinInputPlan(
                name=str(ctx.filename),
                connector=connector,
                rows=rows,
                estimated_bytes=estimated,
                scope=DataScope.FULL,
                freshness=DataFreshness.SNAPSHOT,
                transfer_required=False,
                variant=variant,
                relation_ref=_relation_ref(ctx, variant),
                fingerprint=join_context_fingerprint(ctx),
            )
        else:
            if not table_adapters.supports_pushdown(meta):
                raise JoinInputError(
                    f"The connection for '{ctx.filename}' is unavailable. Reconnect it before "
                    "running a full-data join."
                )
            try:
                adapter = table_adapters.resolve_adapter(ctx, variant)
                rows = int(adapter.count().value)
            except Exception as exc:  # noqa: BLE001
                raise JoinInputError(
                    f"Could not count the complete input '{ctx.filename}': {exc}"
                ) from exc
            estimated = rows * _frame_bytes_per_row(frame)
            item = JoinInputPlan(
                name=str(ctx.filename),
                connector=connector,
                rows=rows,
                estimated_bytes=estimated,
                scope=DataScope.FULL,
                freshness=DataFreshness.LIVE,
                transfer_required=True,
                variant=variant,
                relation_ref=_relation_ref(ctx, variant),
                fingerprint=join_context_fingerprint(ctx),
            )
            transferred += estimated
        planned.append(item)
    return FullJoinInputPlan(tuple(planned), transferred, byte_limit, row_limit)


def materialize_full_join_datasets(
    contexts: Iterable[Any],
    plan: FullJoinInputPlan,
    *,
    approved_over_budget: bool = False,
) -> list[dict[str, Any]]:
    """Return complete DataFrames matching *plan*, streaming connected inputs.

    ``approved_over_budget`` is set only after the confirmation prompt showed
    the plan's transfer/row estimates.  A stale plan is rejected by name and
    connector checks rather than loading a different asset accidentally.
    """
    context_list = list(contexts)
    if len(context_list) != len(plan.inputs):
        raise JoinInputError("The attached datasets changed after the join was proposed.")
    if plan.over_budget and not approved_over_budget:
        raise JoinInputError(
            "This full-data join exceeds the configured transfer or row budget and needs "
            "explicit approval."
        )

    datasets: list[dict[str, Any]] = []
    for ctx, item in zip(context_list, plan.inputs):
        meta = getattr(ctx, "metadata", None) or {}
        connector = str(meta.get("source") or "upload")
        if str(ctx.filename) != item.name or connector != item.connector:
            raise JoinInputError("The attached datasets changed after the join was proposed.")
        if _variant(ctx) != item.variant:
            raise JoinInputError("A working copy changed after the join was proposed.")
        if _relation_ref(ctx, item.variant) != item.relation_ref:
            raise JoinInputError("A source relation changed after the join was proposed.")
        if join_context_fingerprint(ctx) != item.fingerprint:
            raise JoinInputError("A join input changed after the join was proposed.")
        if connector == "upload":
            frame = getattr(ctx, "working_dataframe", None)
            if frame is None:
                frame = getattr(ctx, "dataframe", None)
            if frame is None:
                raise JoinInputError(f"Dataset '{item.name}' no longer has data.")
        else:
            try:
                batches = list(_iter_source_batches(ctx, item.variant, expected_rows=item.rows))
            except Exception as exc:  # noqa: BLE001
                raise JoinInputError(
                    f"Could not stream the complete input '{item.name}': {exc}"
                ) from exc
            if batches:
                frame = pd.concat(batches, ignore_index=True)
            else:
                columns = [
                    column.name
                    for column in table_adapters.resolve_adapter(ctx, item.variant).schema()
                ]
                frame = pd.DataFrame(columns=columns)
            if len(frame) != item.rows:
                raise JoinInputError(
                    f"'{item.name}' changed while it was being read: expected {item.rows:,} "
                    f"rows, received {len(frame):,}. Re-plan the join against a fresh snapshot."
                )
        datasets.append(
            {
                "data": frame,
                "name": item.name,
                "format": None,
                "metadata": {
                    **meta,
                    "join_input_scope": DataScope.FULL.value,
                    "join_input_freshness": item.freshness.value,
                },
            }
        )
    return datasets

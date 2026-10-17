"""Decide where a join between session datasets runs.

The deciding fact is *source identity*, not connector id: relations reachable
through one connection can be joined by that engine at full scale, and anything
else has to be materialized. Adding a source-side connector therefore means
registering a scratch runner and a source-identity builder — not editing this
module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from agents.sql_agent.protocols import supports_source_execution
from datasources.source_identity import source_key

logger = logging.getLogger(__name__)

SOURCE = "source"
MATERIALIZED = "materialized"


@dataclass(frozen=True)
class JoinRelation:
    """One side of a join, resolved to the relation that should actually be read."""

    ctx: Any
    name: str
    schema: Optional[str]
    table: Optional[str]
    custom_query: Optional[str]
    database: Optional[str] = None

    @property
    def is_physical(self) -> bool:
        return bool(self.schema and self.table)


@dataclass(frozen=True)
class JoinPlan:
    mode: str
    reason: str
    relations: tuple[JoinRelation, ...]
    source_key: Optional[str] = None
    connector: Optional[str] = None

    @property
    def source_side(self) -> bool:
        return self.mode == SOURCE


def _relation_for(ctx: Any) -> JoinRelation:
    """The relation to read for *ctx*: its active working copy when one exists,
    otherwise the user's own table or custom query."""
    meta = ctx.metadata or {}
    temp_schema, temp_table = meta.get("temp_schema"), meta.get("temp_table")
    if temp_schema and temp_table:
        return JoinRelation(
            ctx,
            ctx.filename,
            temp_schema,
            temp_table,
            None,
            str(meta.get("scratch_catalog") or meta.get("database") or "") or None,
        )
    custom_query = meta.get("custom_query")
    if custom_query:
        return JoinRelation(ctx, ctx.filename, None, None, str(custom_query))
    if meta.get("source") == "mongodb":
        return JoinRelation(
            ctx,
            ctx.filename,
            str(meta.get("database") or ""),
            meta.get("collection"),
            None,
            str(meta.get("database") or "") or None,
        )
    return JoinRelation(
        ctx,
        ctx.filename,
        str(meta.get("schema") or "public"),
        meta.get("table"),
        None,
        str(meta.get("database") or "") or None,
    )


def plan_join(contexts: Sequence[Any]) -> JoinPlan:
    """Choose the execution mode for joining *contexts* (two or more)."""
    relations = tuple(_relation_for(ctx) for ctx in contexts)
    if len(contexts) < 2:
        return JoinPlan(MATERIALIZED, "single_dataset", relations)

    keys = [source_key(getattr(ctx, "metadata", None)) for ctx in contexts]
    if any(key is None for key in keys):
        # Either a file dataset, a connector with no source-side runner, or a
        # session whose credentials were redacted on restore.
        return JoinPlan(MATERIALIZED, "not_all_source_backed", relations)
    if len(set(keys)) > 1:
        return JoinPlan(MATERIALIZED, "different_sources", relations)
    connectors = {
        str((getattr(ctx, "metadata", None) or {}).get("source") or "") for ctx in contexts
    }
    if len(connectors) != 1:
        return JoinPlan(MATERIALIZED, "different_connectors", relations)
    connector = next(iter(connectors))
    if connector != "mongodb" and not all(
        supports_source_execution(getattr(ctx, "metadata", None)) for ctx in contexts
    ):
        return JoinPlan(MATERIALIZED, "no_source_runner", relations)
    if not all(rel.is_physical or rel.custom_query for rel in relations):
        return JoinPlan(MATERIALIZED, "unresolved_relation", relations)
    return JoinPlan(
        SOURCE,
        "same_source",
        relations,
        source_key=keys[0],
        connector=connector,
    )

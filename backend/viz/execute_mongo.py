"""
Run a QueryPlan against a MongoDB collection.

``viz.compile_mongo`` builds the pipelines; this decides how many to run. A
plain chart is one aggregation. Bins add a stats pass and topK adds one pass per
ranked dimension, because an aggregation cannot join a whole-collection
statistic back onto each document the way SQL's ``CROSS JOIN stats`` does.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from .catalog import FieldCatalog
from .compile_mongo import (
    build_stats_pipeline,
    build_topk_pipeline,
    compile_spec_mongo,
    stats_from_record,
)
from .plan import QueryPlan

logger = logging.getLogger(__name__)


def mongo_target(meta: Dict[str, Any]) -> Tuple[str, str]:
    """The namespace a chart reads: the working copy when one exists, else the
    source collection. Charts never write, so either is safe to read."""
    if meta.get("temp_schema") and meta.get("temp_table"):
        return str(meta["temp_schema"]), str(meta["temp_table"])
    return str(meta.get("database") or ""), str(meta.get("collection") or "")


def run_mongo_plan(
    runner: Any,
    plan: QueryPlan,
    catalog: FieldCatalog,
    *,
    database: str,
    collection: str,
) -> List[Dict[str, Any]]:
    """Execute *plan* and return the raw result records."""
    stats: Dict[str, Dict[str, Any]] = {}
    stats_pipeline = build_stats_pipeline(plan, catalog)
    if stats_pipeline:
        records = runner.run_pipeline(collection, stats_pipeline, database=database)
        stats = stats_from_record(plan, records[0] if records else None)

    tops: Dict[str, List[Any]] = {}
    for dim in plan.dims:
        if dim.top_k is None:
            continue
        # Each ranked dimension is cut against the same filtered input; ranking
        # them in sequence would let one dimension's cut change another's.
        ranked = runner.run_pipeline(
            collection, build_topk_pipeline(plan, catalog, dim, stats), database=database
        )
        tops[dim.channel] = [row["_id"] for row in ranked]

    pipeline = compile_spec_mongo(plan, catalog, stats=stats, top_values=tops)
    # DEBUG, not INFO: the pipeline embeds user filter values.
    logger.debug("[viz] executing spec pipeline: %s", pipeline)
    return runner.run_pipeline(collection, pipeline, database=database)

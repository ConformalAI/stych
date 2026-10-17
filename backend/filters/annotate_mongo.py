"""
Annotate stage for MongoDB — derived fields keyed by document identity.

The document counterpart to ``filters.annotate_sql``. Where that engine emits
``LEFT JOIN``s, this emits pipeline stages that the select stage then reads as
ordinary fields, so the two engines agree on what an annotation *is* even though
they disagree on how to compute one.

Two of the three step kinds run entirely inside the customer's cluster:

- **duplicate_values** is a ``$setWindowFields`` partition — full pushdown, no
  materialization, nothing sampled. This is the one place documents come out
  ahead of the warehouses, which need a grouped subquery joined back.
- **fuzzy_duplicates** cannot be pushed down anywhere: transitive union-find
  needs every value in memory at once. Values are sampled, grouped in Python,
  and written back to a scratch collection joined on ``_id`` — no row id has to
  be stamped, because MongoDB already has one.

  It does need the filtered collection to live in the scratch database, because
  ``$lookup`` cannot cross databases. The SQL engines get this for free: their
  annotation tables sit in a ``stych_temp`` *schema* of the same database as the
  source. MongoDB has no such nesting, so the equivalent is a working copy. A
  filter on the live collection reports that rather than silently producing
  empty groups.
- **sql** steps are refused rather than reinterpreted; a collection has no SQL
  to run, and quietly skipping a step the user wrote would be worse.

Every materialized collection is cached on the annotate fingerprint, so editing
an unrelated part of a filter does not recompute a fuzzy pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from filters import builtins as fb
from filters.spec import (
    DuplicateValuesStep,
    FilterSpec,
    FuzzyDuplicatesStep,
    PandasStep,
    SqlStep,
)

logger = logging.getLogger(__name__)

#: MongoDB's own identity field, used directly as the annotation join key.
ROW_KEY = "_id"
#: Prefix for every scratch collection this module creates.
ANNOT_PREFIX = "stych_annot_"
#: Most materialized annotation collections kept before the oldest is dropped.
_REGISTRY_LIMIT = 32

_registry: "OrderedDict[Tuple, str]" = OrderedDict()


@dataclass
class AnnotationStages:
    """One step's pipeline stages and the fields they contribute."""

    step_id: str
    stages: List[Dict[str, Any]] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)


def _status(step_id: str, status: str, **extra: Any) -> Dict[str, Any]:
    return {"step_id": step_id, "status": status, **extra}


def _fingerprint(step) -> str:
    payload = json.dumps(step.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def collection_name(session_id: str, step_id: str, fingerprint: str) -> str:
    safe_step = "".join(c if c.isalnum() else "_" for c in step_id)[:40]
    return f"{session_collection_prefix(session_id)}{safe_step}_{fingerprint}"


def session_collection_prefix(session_id: str) -> str:
    """Prefix shared by every materialized annotation for one session."""
    safe = "".join(c if c.isalnum() else "_" for c in session_id)[:40]
    # The delimiter prevents session ``abc`` from matching collections owned by
    # session ``abcd`` during lifecycle cleanup.
    return f"{ANNOT_PREFIX}{safe}_"


# ---------------------------------------------------------------------------
# duplicate_values — full pushdown
# ---------------------------------------------------------------------------


def duplicate_values_stages(step: DuplicateValuesStep, known: Sequence[str]) -> AnnotationStages:
    """Exact-duplicate grouping as a window partition over the whole collection.

    A row whose group columns hold a null is not a duplicate of another such
    row — a shared *missing* value is not a shared value — so those rows get
    null outputs, matching the SQL engine's ``WHERE ... IS NOT NULL`` subquery.
    """
    missing = [c for c in step.columns if c not in set(known)]
    if missing:
        raise UnknownAnnotationColumn(missing[0])

    count_out, group_out = step.outputs[0].name, step.outputs[1].name
    counter = f"__stych_n_{step.id}"
    present = [{"$ne": [{"$ifNull": [f"${c}", None]}, None]} for c in step.columns]
    qualifies = {
        "$and": [
            *present,
            {"$gte": [f"${counter}", int(step.min_count)]},
        ]
    }
    return AnnotationStages(
        step_id=step.id,
        stages=[
            {
                "$setWindowFields": {
                    "partitionBy": {f"c{i}": f"${c}" for i, c in enumerate(step.columns)},
                    "output": {counter: {"$count": {}}},
                }
            },
            {
                "$addFields": {
                    count_out: {"$cond": [qualifies, f"${counter}", None]},
                    # The partition key is constant within a partition, so the
                    # first column's own value identifies the group.
                    group_out: {"$cond": [qualifies, {"$toString": f"${step.columns[0]}"}, None]},
                }
            },
            {"$project": {counter: 0}},
        ],
        outputs=[count_out, group_out],
    )


class UnknownAnnotationColumn(ValueError):
    """An annotate step references a field absent from the sampled schema."""

    def __init__(self, column: str):
        self.column = column
        super().__init__(f"Unknown column {column!r}")


# ---------------------------------------------------------------------------
# fuzzy_duplicates — sampled, computed in Python, written back to scratch
# ---------------------------------------------------------------------------


def _materialize_fuzzy(
    runner,
    step: FuzzyDuplicatesStep,
    *,
    database: str,
    collection: str,
    target: str,
) -> Tuple[int, bool]:
    """Sample → union-find → scratch collection. Returns ``(documents, degraded)``."""
    import pandas as pd  # noqa: PLC0415

    client = runner._client()
    total = client[database][collection].count_documents({})
    degraded = total > step.max_rows
    documents = list(
        client[database][collection].find({}, {ROW_KEY: 1, step.on: 1}, limit=int(step.max_rows))
    )
    index = pd.Index([d.get(ROW_KEY) for d in documents])
    values = pd.Series([d.get(step.on) for d in documents], index=index)
    produced, _capped = fb.fuzzy_duplicate_groups(
        values, strategy=step.strategy, threshold=step.threshold, max_rows=step.max_rows
    )
    frame = produced.rename(
        columns={
            fb.GROUP_COL: step.outputs[0].name,
            fb.SCORE_COL: step.outputs[1].name,
            fb.MATCH_KEY_COL: step.outputs[2].name,
        }
    )
    frame = frame[frame.notna().any(axis=1)]

    records = []
    for key, row in frame.iterrows():
        record = {ROW_KEY: key}
        for name in (o.name for o in step.outputs):
            value = row.get(name)
            record[name] = None if pd.isna(value) else value
        records.append(record)

    handle = client[runner.temp_schema][target]
    handle.drop()
    if records:
        handle.insert_many(records)
    return len(records), degraded


def fuzzy_stages(step: FuzzyDuplicatesStep, scratch_db: str, target: str) -> AnnotationStages:
    """Join the materialized groups back on ``_id``."""
    names = [o.name for o in step.outputs]
    alias = f"__stych_a_{step.id}"
    return AnnotationStages(
        step_id=step.id,
        stages=[
            {
                "$lookup": {
                    "from": target,
                    "localField": ROW_KEY,
                    "foreignField": ROW_KEY,
                    "as": alias,
                }
            },
            {"$addFields": {n: {"$ifNull": [{"$first": f"${alias}.{n}"}, None]} for n in names}},
            {"$project": {alias: 0}},
        ],
        outputs=names,
    )


def _remember(runner, key: Tuple, target: str) -> None:
    """Track materialized collections so the scratch database cannot grow without bound."""
    _registry[key] = target
    _registry.move_to_end(key)
    while len(_registry) > _REGISTRY_LIMIT:
        _stale_key, stale = _registry.popitem(last=False)
        try:
            runner._client()[runner.temp_schema][stale].drop()
        except Exception as exc:  # noqa: BLE001 — best-effort reclaim
            logger.warning("[annotate] could not drop %s: %s", stale, exc)


def clear_registry() -> None:
    _registry.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_annotation_stages(
    runner,
    spec: FilterSpec,
    known: Sequence[str],
    *,
    database: str,
    collection: str,
    session_id: str,
    temp_generation: int = 0,
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]], Optional[str]]:
    """Compile every annotate step.

    Returns ``(stages, output_names, statuses, degraded)``. Steps are applied in
    declaration order, and each one's outputs are visible to the next.
    """
    stages: List[Dict[str, Any]] = []
    outputs: List[str] = []
    statuses: List[Dict[str, Any]] = []
    degraded: Optional[str] = None
    visible = list(known)

    for step in spec.annotate:
        if isinstance(step, DuplicateValuesStep):
            try:
                built = duplicate_values_stages(step, visible)
            except UnknownAnnotationColumn as exc:
                statuses.append(
                    _status(step.id, "skipped", reason=f"unknown column {exc.column!r}")
                )
                continue
            stages.extend(built.stages)
            outputs.extend(built.outputs)
            visible.extend(built.outputs)
            statuses.append(_status(step.id, "ok", scope="full"))
            continue

        if isinstance(step, FuzzyDuplicatesStep):
            if step.on not in set(visible):
                statuses.append(_status(step.id, "skipped", reason=f"unknown column {step.on!r}"))
                continue
            if database != runner.temp_schema:
                # $lookup cannot cross databases, so groups materialized into
                # scratch are invisible from the live collection. Joining anyway
                # would return empty groups that look like "no duplicates".
                statuses.append(
                    _status(
                        step.id,
                        "unsupported",
                        reason=(
                            "Fuzzy duplicate grouping needs a working copy: MongoDB cannot "
                            "join across databases, and the groups are stored in the Stych "
                            "scratch database."
                        ),
                    )
                )
                continue
            fingerprint = _fingerprint(step)
            target = collection_name(session_id, step.id, fingerprint)
            key = (
                runner.credential_key,
                database,
                collection,
                int(temp_generation),
                step.id,
                fingerprint,
            )
            if _registry.get(key) != target:
                try:
                    _rows, step_degraded = _materialize_fuzzy(
                        runner,
                        step,
                        database=database,
                        collection=collection,
                        target=target,
                    )
                except Exception as exc:  # noqa: BLE001
                    statuses.append(_status(step.id, "failed", reason=str(exc)))
                    continue
                _remember(runner, key, target)
                if step_degraded:
                    degraded = "sampled_annotations"
            built = fuzzy_stages(step, runner.temp_schema, target)
            stages.extend(built.stages)
            outputs.extend(built.outputs)
            visible.extend(built.outputs)
            statuses.append(_status(step.id, "ok", scope="sample" if degraded else "full"))
            continue

        if isinstance(step, (SqlStep, PandasStep)):
            # Refused, not reinterpreted: a collection has no SQL to run and no
            # parquet to hand a sandbox, and silently skipping a step the user
            # wrote would leave the filter looking like it worked.
            statuses.append(
                _status(
                    step.id,
                    "unsupported",
                    reason=(
                        f"{step.kind} annotation steps are not available for MongoDB sources; "
                        "use duplicate_values or fuzzy_duplicates."
                    ),
                )
            )
            continue

    return stages, outputs, statuses, degraded

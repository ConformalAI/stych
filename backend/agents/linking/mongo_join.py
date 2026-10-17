"""Full-data exact joins for two MongoDB collections.

This module is deliberately independent of the general join planner.  The
planner can later adapt its ``JoinRelation`` objects to :class:`MongoRelation`
and call :func:`execute_mongo_join`; keeping the aggregation compiler here
prevents MongoDB from being treated as a SQL dialect.

Only collections that are reachable from one MongoDB database are eligible.
The aggregation reads both collections in MongoDB and writes its result to a
Stych-owned scratch collection.  No source collection is modified and no
sample is used as a completed join.

The output documents are intentionally namespaced as ``left`` and ``right``.
``datasources.mongodb.flatten_documents`` consequently exposes stable dotted
columns such as ``left.customer_id`` and ``right.customer_id`` while retaining
duplicate field names from both sides.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from agents.mongo_agent.pipeline_guard import validate_document_expression
from agents.mongo_agent.runner import MongoScratchRunner
from datasources.mongodb import flatten_documents

MONGO_INNER = "INNER"
MONGO_LEFT = "LEFT"
MONGO_RIGHT = "RIGHT"
MONGO_FULL = "FULL OUTER"
VALID_MONGO_JOIN_TYPES = frozenset({MONGO_INNER, MONGO_LEFT, MONGO_RIGHT, MONGO_FULL})

_DB_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Mongo collection names may contain dots and dashes.  Reject system names and
# control/operator characters; relation names come from attached metadata, not
# raw agent-authored pipeline input.
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}$")
_FIELD_RE = re.compile(r"^[^$\x00.][^$\x00]{0,255}(?:\.[^$\x00]{1,255})*$")

MAX_PREVIEW_ROWS = 500
logger = logging.getLogger(__name__)


class MongoJoinError(ValueError):
    """The requested MongoDB join is invalid or not safely executable."""


@dataclass(frozen=True)
class MongoRelation:
    """An attached MongoDB collection resolved to a physical relation.

    ``base_query`` is the immutable filter captured when a collection was
    attached.  It is applied before matching and is never accepted as an
    aggregation pipeline supplied by the agent.
    """

    database: str
    collection: str
    connection_url: str
    base_query: Optional[Mapping[str, Any]] = None
    name: Optional[str] = None
    columns: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MongoJoinEligibility:
    """Machine-readable eligibility result for the planner and UI."""

    eligible: bool
    reason: str
    message: str


@dataclass
class MongoJoinResult:
    """A scratch-backed join result and a bounded UI preview."""

    database: str
    collection: str
    rows: int
    columns: List[str]
    preview: pd.DataFrame
    pipeline: List[Dict[str, Any]] = field(default_factory=list)
    join_type: str = MONGO_INNER
    strategy: str = "mongo_lookup_exact"


def _validate_database(value: str) -> str:
    text = str(value or "").strip()
    if not _DB_RE.fullmatch(text):
        raise MongoJoinError(f"MongoDB database {value!r} is not a safe attached relation.")
    return text


def _validate_collection(value: str) -> str:
    text = str(value or "").strip()
    if not _COLLECTION_RE.fullmatch(text) or text.startswith("system."):
        raise MongoJoinError(f"MongoDB collection {value!r} is not a safe attached relation.")
    return text


def _validate_field(value: Any) -> str:
    text = str(value or "").strip()
    if not _FIELD_RE.fullmatch(text):
        raise MongoJoinError(f"MongoDB join field {value!r} is invalid.")
    return text


def _normalise_join_type(join_type: str) -> str:
    value = str(join_type or MONGO_INNER).upper().replace("  ", " ").strip()
    if value == "FULL":
        value = MONGO_FULL
    if value not in VALID_MONGO_JOIN_TYPES:
        raise MongoJoinError(
            f"MongoDB exact joins support INNER, LEFT, RIGHT, and FULL OUTER; got {join_type!r}."
        )
    return value


def _safe_base_query(query: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if query is None:
        return None
    if not isinstance(query, Mapping):
        raise MongoJoinError("MongoDB attached filters must be an object.")
    # This rejects server-side JavaScript recursively while preserving normal
    # Mongo predicates such as $and, $in, and $exists from the attached source.
    validate_document_expression(query)
    return dict(query)


def validate_mongo_join(
    left: MongoRelation,
    right: MongoRelation,
    keys: Sequence[Tuple[str, str]],
    *,
    join_type: str = MONGO_INNER,
) -> MongoJoinEligibility:
    """Return whether an exact full-data MongoDB join can run natively.

    MongoDB's ``$lookup`` cannot read a collection in another database.  A
    different database therefore receives an explicit ineligible result so the
    caller can choose a full streamed DuckDB fallback instead of sampling.
    """

    try:
        _validate_database(left.database)
        _validate_database(right.database)
        _validate_collection(left.collection)
        _validate_collection(right.collection)
        if not left.connection_url or left.connection_url in {"[REDACTED]", "REDACTED"}:
            raise MongoJoinError("The left MongoDB connection is unavailable; reconnect it.")
        if not right.connection_url or right.connection_url in {"[REDACTED]", "REDACTED"}:
            raise MongoJoinError("The right MongoDB connection is unavailable; reconnect it.")
        if left.connection_url != right.connection_url:
            raise MongoJoinError(
                "The two MongoDB collections use different connections; native $lookup "
                "cannot reach both collections."
            )
        if left.database != right.database:
            raise MongoJoinError(
                "The two MongoDB collections are in different databases; native $lookup "
                "cannot cross databases."
            )
        if not keys:
            raise MongoJoinError("An exact MongoDB join needs at least one key pair.")
        for pair in keys:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise MongoJoinError("Each MongoDB join key must contain a left and right field.")
            _validate_field(pair[0])
            _validate_field(pair[1])
        _normalise_join_type(join_type)
        _safe_base_query(left.base_query)
        _safe_base_query(right.base_query)
    except MongoJoinError as exc:
        return MongoJoinEligibility(False, "ineligible", str(exc))
    return MongoJoinEligibility(
        True,
        "same_database_lookup",
        "Full-data exact join runs inside MongoDB using $lookup; only the result is written to Stych scratch.",
    )


def _match_expr(keys: Sequence[Tuple[str, str]], *, left_side: bool) -> Dict[str, Any]:
    """Build SQL-style equality for one lookup direction.

    Missing and null keys never match. Values are otherwise compared without
    coercion, preserving MongoDB's type-sensitive equality.
    """

    comparisons = []
    for index, pair in enumerate(keys):
        _source_field, foreign_field = pair if left_side else (pair[1], pair[0])
        foreign_ref = f"${foreign_field}"
        source_ref = f"$${'stych_key_' + str(index)}"
        comparisons.append(
            {
                "$and": [
                    {"$ne": [source_ref, None]},
                    {"$ne": [foreign_ref, None]},
                    {"$eq": [foreign_ref, source_ref]},
                ]
            }
        )
    return comparisons[0] if len(comparisons) == 1 else {"$and": comparisons}


def _lookup_stage(
    *,
    from_collection: str,
    keys: Sequence[Tuple[str, str]],
    source_is_left: bool,
    base_query: Optional[Mapping[str, Any]],
    matches_field: str,
) -> Dict[str, Any]:
    let_vars = {
        f"stych_key_{index}": f"${pair[0] if source_is_left else pair[1]}"
        for index, pair in enumerate(keys)
    }
    foreign_expr = _match_expr(keys, left_side=source_is_left)
    pipeline: List[Dict[str, Any]] = []
    if base_query:
        pipeline.append({"$match": dict(base_query)})
    pipeline.append({"$match": {"$expr": foreign_expr}})
    return {
        "$lookup": {
            "from": from_collection,
            "let": let_vars,
            "pipeline": pipeline,
            "as": matches_field,
        }
    }


def _namespace_pipeline(
    *,
    matches_field: str,
    left_shadow: str,
    left_output: str,
    right_output: str,
    preserve_matches: bool,
    source_is_left: bool,
) -> List[Dict[str, Any]]:
    stages: List[Dict[str, Any]] = [
        {
            "$unwind": {
                "path": f"${matches_field}",
                "preserveNullAndEmptyArrays": preserve_matches,
            }
        },
        {"$set": {left_shadow: "$$ROOT"}},
        # The shadow contains the lookup array. Remove it before exposing the
        # document under the left/right namespace.
        {"$unset": f"{left_shadow}.{matches_field}"},
    ]
    if source_is_left:
        left_ref, right_ref = f"${left_shadow}", f"${matches_field}"
    else:
        left_ref, right_ref = f"${matches_field}", f"${left_shadow}"
    stages.append(
        {
            "$set": {
                left_output: left_ref,
                right_output: {"$ifNull": [right_ref, None]},
            }
        }
    )
    stages.append({"$project": {"_id": 0, left_output: 1, right_output: 1}})
    return stages


def build_mongo_join_pipeline(
    left: MongoRelation,
    right: MongoRelation,
    keys: Sequence[Tuple[str, str]],
    *,
    join_type: str = MONGO_INNER,
    output_database: str,
    output_collection: str,
) -> List[Dict[str, Any]]:
    """Compile a guarded aggregation pipeline; does not execute it."""

    eligibility = validate_mongo_join(left, right, keys, join_type=join_type)
    if not eligibility.eligible:
        raise MongoJoinError(eligibility.message)
    output_db = _validate_database(output_database)
    output_coll = _validate_collection(output_collection)
    if not output_coll.startswith("stych_"):
        raise MongoJoinError("MongoDB join output must be a Stych scratch collection.")
    join = _normalise_join_type(join_type)

    # Use per-compilation names to avoid collisions with user fields. The
    # fields are removed before the output is published.
    token = uuid.uuid4().hex[:10]
    matches = f"__stych_join_matches_{token}"
    shadow = f"__stych_join_left_{token}"
    left_output = "left"
    right_output = "right"

    source_is_left = join != MONGO_RIGHT
    anchor = left if source_is_left else right
    foreign = right if source_is_left else left
    preserve = join in {MONGO_LEFT, MONGO_RIGHT, MONGO_FULL}
    pipeline: List[Dict[str, Any]] = []
    if anchor.base_query:
        pipeline.append({"$match": dict(_safe_base_query(anchor.base_query) or {})})
    pipeline.append(
        _lookup_stage(
            from_collection=foreign.collection,
            keys=keys,
            source_is_left=source_is_left,
            base_query=_safe_base_query(foreign.base_query),
            matches_field=matches,
        )
    )

    # For a right/full join the right collection is the anchor and the
    # namespace helper reverses the two output documents.
    pipeline.extend(
        _namespace_pipeline(
            matches_field=matches,
            left_shadow=shadow,
            left_output=left_output,
            right_output=right_output,
            preserve_matches=preserve,
            source_is_left=source_is_left,
        )
    )

    if join == MONGO_FULL:
        # Add right-only documents. The left branch already contains every
        # matching and left-only document; this branch contributes only right
        # documents with no matching left key.
        right_only_matches = f"__stych_join_right_only_{token}"
        right_only_shadow = f"__stych_join_right_shadow_{token}"
        right_only_lookup = _lookup_stage(
            from_collection=left.collection,
            keys=keys,
            source_is_left=False,
            base_query=_safe_base_query(left.base_query),
            matches_field=right_only_matches,
        )
        right_only_pipeline: List[Dict[str, Any]] = []
        if right.base_query:
            right_only_pipeline.append({"$match": dict(_safe_base_query(right.base_query) or {})})
        right_only_pipeline.extend(
            [
                right_only_lookup,
                {"$match": {"$expr": {"$eq": [{"$size": f"${right_only_matches}"}, 0]}}},
                {"$set": {right_only_shadow: "$$ROOT"}},
                {"$unset": f"{right_only_shadow}.{right_only_matches}"},
                {
                    "$set": {
                        left_output: None,
                        right_output: f"${right_only_shadow}",
                    }
                },
                {"$project": {"_id": 0, left_output: 1, right_output: 1}},
            ]
        )
        pipeline.append({"$unionWith": {"coll": right.collection, "pipeline": right_only_pipeline}})

    pipeline.append({"$out": {"db": output_db, "coll": output_coll}})
    return pipeline


def execute_mongo_join(
    runner: MongoScratchRunner,
    left: MongoRelation,
    right: MongoRelation,
    keys: Sequence[Tuple[str, str]],
    *,
    session_id: str,
    join_type: str = MONGO_INNER,
    preview_rows: int = MAX_PREVIEW_ROWS,
    timeout_ms: int = 60_000,
) -> MongoJoinResult:
    """Execute a full-data exact join and publish only a scratch collection."""

    if not 0 <= int(preview_rows) <= MAX_PREVIEW_ROWS:
        raise MongoJoinError(f"preview_rows must be between 0 and {MAX_PREVIEW_ROWS}.")
    eligibility = validate_mongo_join(left, right, keys, join_type=join_type)
    if not eligibility.eligible:
        raise MongoJoinError(eligibility.message)
    if (
        runner.connection_url != left.connection_url
        or runner.connection_url != right.connection_url
    ):
        raise MongoJoinError("The MongoDB runner connection does not match both join inputs.")

    output_db = runner.temp_schema
    output_collection = (
        runner.collection_name(session_id) if hasattr(runner, "collection_name") else None
    )
    # MongoScratchRunner exposes collection_name as a module helper, not an
    # instance method. Keep the fallback explicit for small test doubles.
    if not output_collection:
        from agents.mongo_agent.runner import collection_name

        output_collection = collection_name(session_id)
    pipeline = build_mongo_join_pipeline(
        left,
        right,
        keys,
        join_type=join_type,
        output_database=output_db,
        output_collection=output_collection,
    )
    try:
        runner.ensure_temp_schema()
        runner.run_pipeline(
            left.collection if join_type.upper().strip() != MONGO_RIGHT else right.collection,
            pipeline,
            database=left.database,
            timeout_ms=int(timeout_ms),
            allow_other_collections=True,
            allow_writes=True,
        )
        rows = runner.get_row_count(output_db, output_collection)
        preview_docs = runner.run_pipeline(
            output_collection,
            [{"$limit": int(preview_rows)}],
            database=output_db,
            timeout_ms=min(int(timeout_ms), 10_000),
            allow_other_collections=False,
            allow_writes=False,
        )
    except Exception as exc:
        try:
            runner.drop_temp_table(output_collection)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[linking] could not clean up failed MongoDB join result %s",
                output_collection,
            )
        raise MongoJoinError(f"MongoDB exact join failed: {exc}") from exc
    preview = flatten_documents(preview_docs)
    known_columns = [
        *(f"left.{column}" for column in left.columns),
        *(f"right.{column}" for column in right.columns),
    ]
    columns = list(dict.fromkeys([*known_columns, *(str(column) for column in preview.columns)]))
    preview = preview.reindex(columns=columns)
    return MongoJoinResult(
        database=output_db,
        collection=output_collection,
        rows=rows,
        columns=columns,
        preview=preview,
        pipeline=pipeline,
        join_type=_normalise_join_type(join_type),
    )


__all__ = [
    "MAX_PREVIEW_ROWS",
    "MongoJoinEligibility",
    "MongoJoinError",
    "MongoJoinResult",
    "MongoRelation",
    "build_mongo_join_pipeline",
    "execute_mongo_join",
    "validate_mongo_join",
]

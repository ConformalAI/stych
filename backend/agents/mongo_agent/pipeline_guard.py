"""Confine agent-authored aggregation pipelines to the Stych scratch namespace.

The MongoDB counterpart to ``sql_agent.write_guard``. None of the SQL guard
transfers: a pipeline is a list of BSON stages, not a statement, so the checks
are structural rather than textual — which is an advantage. There is no
comment-smuggling or quoting trick to defend against, only a stage list.

Two distinct hazards:

- **Writing stages.** ``$out`` and ``$merge`` write a collection, and both take
  an ``into`` that may name another database. Their target must be a scratch
  collection; anything else is refused.
- **Server-side code.** ``$function``, ``$accumulator`` and ``$where`` execute
  JavaScript inside the customer's database. Nothing Stych generates needs them,
  and an agent-authored pipeline must never be able to reach them, so they are
  refused wherever they appear — including nested inside another stage.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

# Stages that write. Everything else in a pipeline is a read.
WRITE_STAGES = frozenset({"$out", "$merge"})

# Stages that execute JavaScript in the server. Never permitted.
CODE_STAGES = frozenset({"$function", "$accumulator", "$where", "$expr_function"})

# Stages that read another collection. Allowed, but recorded so a caller can
# see what a pipeline touches.
READ_OTHER_STAGES = frozenset({"$lookup", "$unionWith", "$graphLookup"})


class PipelineGuardError(ValueError):
    """A pipeline writes outside the scratch namespace, or executes code."""


def _iter_nested(value: Any) -> Iterable[Any]:
    """Every mapping and sequence inside *value*, including itself."""
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_nested(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_nested(item)


def _target_of(stage_name: str, body: Any) -> Tuple[Optional[str], str]:
    """``(database, collection)`` a write stage targets.

    ``$out`` accepts a bare collection name or ``{db, coll}``; ``$merge``
    accepts either, or ``{into: ...}``. A database of ``None`` means "the
    database the pipeline runs against".
    """
    target = body
    if stage_name == "$merge" and isinstance(body, Mapping):
        target = body.get("into", body)
    if isinstance(target, str):
        return None, target
    if isinstance(target, Mapping):
        return (
            (str(target["db"]) if target.get("db") is not None else None),
            str(target.get("coll") or ""),
        )
    raise PipelineGuardError(f"{stage_name} target could not be read, so the pipeline is refused.")


def write_targets(pipeline: Sequence[Mapping[str, Any]]) -> List[Tuple[Optional[str], str]]:
    """Every ``(database, collection)`` the pipeline writes to."""
    targets: List[Tuple[Optional[str], str]] = []
    for stage in pipeline:
        if not isinstance(stage, Mapping) or len(stage) != 1:
            continue
        name = next(iter(stage))
        if name in WRITE_STAGES:
            targets.append(_target_of(name, stage[name]))
    return targets


def validate_pipeline(
    pipeline: Sequence[Mapping[str, Any]],
    *,
    scratch_db: str,
    scratch_prefix: str,
    running_db: Optional[str] = None,
    allow_other_collections: bool = True,
    allow_writes: bool = True,
) -> None:
    """Raise unless *pipeline* is safe to run against the customer's cluster.

    A write must land in *scratch_db* on a collection carrying *scratch_prefix*,
    so a pipeline cannot overwrite the customer's own collection even inside the
    scratch database.
    """
    if not isinstance(pipeline, (list, tuple)):
        raise PipelineGuardError("A pipeline must be a list of stages.")

    for stage in pipeline:
        if not isinstance(stage, Mapping):
            raise PipelineGuardError("Every pipeline stage must be a document.")
        if len(stage) != 1:
            raise PipelineGuardError(
                f"A pipeline stage must have exactly one operator (got {sorted(stage)})."
            )

    # Code execution can hide anywhere, not just at stage level.
    for node in _iter_nested(list(pipeline)):
        if isinstance(node, Mapping):
            for key in node:
                if isinstance(key, str) and key in CODE_STAGES:
                    raise PipelineGuardError(
                        f"{key} runs JavaScript inside your database and is not permitted."
                    )

    for index, stage in enumerate(pipeline):
        name = next(iter(stage))
        if name in READ_OTHER_STAGES and not allow_other_collections:
            raise PipelineGuardError(
                f"{name} reads another collection. Attach that collection explicitly; "
                "agent queries are confined to this chat's selected collection."
            )
        if name not in WRITE_STAGES:
            continue
        if not allow_writes:
            raise PipelineGuardError(f"{name} writes data and is not permitted in a read tool.")
        if index != len(pipeline) - 1:
            # Mongo requires this too; saying so plainly beats a driver error.
            raise PipelineGuardError(f"{name} must be the last stage of a pipeline.")
        database, collection = _target_of(name, stage[name])
        effective_db = database or running_db
        if effective_db != scratch_db:
            raise PipelineGuardError(
                f"{name} may only write to the '{scratch_db}' database "
                f"(got {effective_db or 'the current database'})."
            )
        if not collection.startswith(scratch_prefix):
            raise PipelineGuardError(
                f"{name} may only write to Stych scratch collections "
                f"(expected a name starting with '{scratch_prefix}', got {collection!r})."
            )


def validate_document_expression(value: Any) -> None:
    """Reject server-side JavaScript in filters and update expressions."""
    for node in _iter_nested(value):
        if not isinstance(node, Mapping):
            continue
        for key in node:
            if isinstance(key, str) and key in CODE_STAGES:
                raise PipelineGuardError(
                    f"{key} runs JavaScript inside MongoDB and is not permitted."
                )


def assert_scratch_collection(name: str, *, scratch_prefix: str) -> str:
    """Guard for the direct write paths (``updateMany``, ``drop``) that bypass
    the aggregation pipeline entirely."""
    if not name.startswith(scratch_prefix):
        raise PipelineGuardError(
            f"{name!r} is not a Stych scratch collection; writes are confined to "
            f"names starting with '{scratch_prefix}'."
        )
    return name

"""Physical execution boundary for a confirmed agent-authored join plan."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from agents.sql_agent import working_copies as wc
from datasources.join_planner import JoinPlan, plan_join
from execution.join_inputs import (
    JoinInputError,
    join_context_fingerprint,
    join_input_plan_from_payload,
    join_input_plan_payload,
    materialize_full_join_datasets,
    plan_full_join_inputs,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeJoinResult:
    table: str
    rows: int
    columns: List[str]
    step_log: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    connector: str
    preview: pd.DataFrame


def _refresh_planning_frame(ctx: Any, preview: pd.DataFrame, total_rows: int) -> None:
    """Keep the agent's bounded planning frame aligned with the active scratch result."""
    frame = preview.copy(deep=True)
    ctx.working_dataframe = frame
    meta = ctx.metadata
    meta["snapshot_rows"] = len(frame)
    meta["snapshot_total_rows"] = int(total_rows)
    meta["snapshot_truncated"] = int(total_rows) > len(frame)
    meta["column_samples"] = {
        str(column): list(frame[column].dropna().astype(str).unique()[:5])
        for column in frame.columns[:20]
    }


def native_join_eligible(plan: JoinPlan, steps: Sequence[Dict[str, Any]]) -> bool:
    if not plan.source_side or not steps:
        return False
    if any(str(step.get("strategy") or "exact").lower() != "exact" for step in steps):
        return False
    if plan.connector == "mongodb":
        return len(plan.relations) == 2 and len(steps) == 1
    return True


def plan_physical_join(
    contexts: Sequence[Any],
    steps: Sequence[Dict[str, Any]],
    resolved_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Plan native execution and its explicitly confirmed full-data fallback."""
    source_plan = plan_join(contexts)
    native = native_join_eligible(source_plan, steps)
    input_payload: Optional[Dict[str, Any]] = None
    fallback_error: Optional[str] = None
    try:
        input_payload = join_input_plan_payload(plan_full_join_inputs(contexts))
    except JoinInputError as exc:
        fallback_error = str(exc)
        if not native:
            raise
    return {
        "native_primary": native,
        "connector": source_plan.connector,
        "input_plan": input_payload,
        "fallback_error": fallback_error,
        "resolved": resolved_payload,
        "context_fingerprints": [join_context_fingerprint(ctx) for ctx in contexts],
    }


def validate_confirmed_contexts(contexts: Sequence[Any], physical_plan: Dict[str, Any]) -> None:
    expected = physical_plan.get("context_fingerprints")
    current = [join_context_fingerprint(ctx) for ctx in contexts]
    if not isinstance(expected, list) or current != expected:
        raise JoinInputError(
            "The join inputs or active source relations changed after confirmation."
        )


def validate_confirmed_physical_plan(
    contexts: Sequence[Any],
    steps: Sequence[Dict[str, Any]],
    physical_plan: Dict[str, Any],
) -> None:
    """Ensure native/fallback routing still describes the confirmed inputs."""
    validate_confirmed_contexts(contexts, physical_plan)
    current = plan_join(contexts)
    if bool(physical_plan.get("native_primary")) != native_join_eligible(current, steps):
        raise JoinInputError("Join execution eligibility changed after confirmation.")
    if physical_plan.get("connector") != current.connector:
        raise JoinInputError("The join connector changed after confirmation.")


def materialize_confirmed_inputs(
    contexts: Sequence[Any], physical_plan: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Revalidate a confirmed input fingerprint, then stream complete inputs."""
    confirmed_payload = physical_plan.get("input_plan")
    validate_confirmed_contexts(contexts, physical_plan)
    if not confirmed_payload:
        raise JoinInputError("The full-data transfer fallback was not part of the confirmed plan.")
    confirmed = join_input_plan_from_payload(confirmed_payload)
    current = plan_full_join_inputs(
        contexts,
        max_input_bytes=confirmed.max_input_bytes,
        max_input_rows=confirmed.max_input_rows,
    )
    if join_input_plan_payload(current) != confirmed_payload:
        raise JoinInputError(
            "The source row counts or active relations changed after confirmation."
        )
    return materialize_full_join_datasets(
        contexts,
        current,
        approved_over_budget=True,
    )


def _relation_index(ref: Any, plan: JoinPlan) -> int:
    if isinstance(ref, int) and 0 <= ref < len(plan.relations):
        return ref
    for index, relation in enumerate(plan.relations):
        if relation.name == str(ref):
            return index
    raise ValueError(f"Cannot resolve join dataset {ref!r}.")


def _execute_mongo_join(plan: JoinPlan, step: Dict[str, Any], session_id: str) -> NativeJoinResult:
    from agents.linking.mongo_join import MongoRelation, execute_mongo_join  # noqa: PLC0415
    from agents.mongo_agent.runner import MongoScratchRunner  # noqa: PLC0415

    left_relation = plan.relations[_relation_index(step.get("left"), plan)]
    right_relation = plan.relations[_relation_index(step.get("right"), plan)]

    def mongo_relation(relation: Any) -> MongoRelation:
        meta = relation.ctx.metadata or {}
        frame = getattr(relation.ctx, "working_dataframe", None)
        if frame is None:
            frame = getattr(relation.ctx, "dataframe", None)
        is_base = relation.schema == meta.get("database") and relation.table == meta.get(
            "collection"
        )
        return MongoRelation(
            database=str(relation.schema or ""),
            collection=str(relation.table or ""),
            connection_url=str(meta.get("connection_url") or ""),
            base_query=meta.get("query") if is_base else None,
            name=relation.name,
            columns=tuple(str(column) for column in getattr(frame, "columns", ())),
        )

    key_pairs = step.get("keys") or [{"left": step.get("left_on"), "right": step.get("right_on")}]
    runner = MongoScratchRunner.from_meta(plan.relations[0].ctx.metadata)
    meta = plan.relations[0].ctx.metadata
    wc.adopt_legacy(meta)
    if len(wc.list_copies(meta)) >= wc.MAX_COPIES:
        raise ValueError(
            f"Working-copy limit reached ({wc.MAX_COPIES}). Drop a copy before joining."
        )
    copy_name = wc.default_name(meta)
    result = execute_mongo_join(
        runner,
        mongo_relation(left_relation),
        mongo_relation(right_relation),
        [(str(pair["left"]), str(pair["right"])) for pair in key_pairs],
        session_id=session_id or "join",
        join_type=str(step.get("join_type") or "INNER"),
    )
    try:
        wc.register_copy(
            meta,
            name=copy_name,
            table=result.collection,
            parent=wc.SOURCE_PARENT,
        )
    except wc.WorkingCopyError as exc:
        try:
            runner.drop_temp_table(result.collection)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[linking] could not clean up unregistered MongoDB join result %s",
                result.collection,
            )
        raise ValueError(f"Could not register the MongoDB join working copy: {exc}") from exc
    _refresh_planning_frame(plan.relations[0].ctx, result.preview, result.rows)
    return NativeJoinResult(
        table=f"{result.database}.{result.collection}",
        rows=result.rows,
        columns=result.columns,
        step_log=[
            {
                "left": left_relation.name,
                "right": right_relation.name,
                "join_type": result.join_type,
                "strategy": "exact",
                "executed_in": "mongodb",
            }
        ],
        metadata=meta,
        connector="mongodb",
        preview=result.preview,
    )


def execute_native_join(
    plan: JoinPlan,
    steps: Sequence[Dict[str, Any]],
    *,
    session_id: str,
    state: Any,
) -> NativeJoinResult:
    """Execute exact joins in SQL or MongoDB scratch without input egress."""
    if not native_join_eligible(plan, steps):
        raise ValueError("This join is not eligible for native source execution.")
    if plan.connector == "mongodb":
        return _execute_mongo_join(plan, steps[0], session_id)

    from agents.linking.source_join import execute_source_join  # noqa: PLC0415

    result = execute_source_join(plan, steps, session_id=session_id, state=state)
    meta = plan.relations[0].ctx.metadata
    _refresh_planning_frame(plan.relations[0].ctx, result.preview, result.rows)
    return NativeJoinResult(
        table=f"{result.schema}.{result.table}",
        rows=result.rows,
        columns=result.columns,
        step_log=result.step_log,
        metadata=meta,
        connector=plan.connector or "source",
        preview=result.preview,
    )


__all__ = [
    "NativeJoinResult",
    "execute_native_join",
    "materialize_confirmed_inputs",
    "native_join_eligible",
    "plan_physical_join",
    "validate_confirmed_contexts",
    "validate_confirmed_physical_plan",
]

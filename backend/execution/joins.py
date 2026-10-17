"""Exact joins over normalized session snapshots."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pyarrow as pa

from datasources.contracts.joins import CROSS_SOURCE_UNAVAILABLE_MESSAGE
from datasources.registry import SOURCE_WORKING_COPY_IDS
from execution.duckdb.naming import quote_identifier, validate_relation_name
from execution.session_plane import PlaneKind, resolve_data_plane

_JOIN_TYPES = {
    "INNER": "INNER",
    "LEFT": "LEFT",
    "LEFT OUTER": "LEFT",
    "RIGHT": "RIGHT",
    "RIGHT OUTER": "RIGHT",
    "FULL": "FULL OUTER",
    "FULL OUTER": "FULL OUTER",
    "OUTER": "FULL OUTER",
}


def _resolve_ref(ref: Any, names: list[str], relations: dict[str, str]) -> str | None:
    if isinstance(ref, int) and 0 <= ref < len(names):
        return relations.get(names[ref])
    return relations.get(str(ref))


def _column_names(kernel: Any, relation: str) -> list[str]:
    result = kernel.query(f"SELECT * FROM {quote_identifier(relation)} LIMIT 0", max_rows=1)
    return list(result.columns)


def _join_projection(
    left_columns: list[str],
    right_columns: list[str],
    key_pairs: list[tuple[str, str]],
) -> str:
    projections = []
    coalesced_keys = {left for left, right in key_pairs if left == right}
    for column in left_columns:
        quoted = quote_identifier(column)
        if column in coalesced_keys:
            projections.append(f"COALESCE(l.{quoted}, r.{quoted}) AS {quoted}")
        else:
            projections.append(f"l.{quoted}")
    occupied = set(left_columns)
    for column in right_columns:
        if column in coalesced_keys:
            continue
        alias = column
        if alias in occupied:
            alias = f"{column}_right"
        suffix = 2
        while alias in occupied:
            alias = f"{column}_right_{suffix}"
            suffix += 1
        occupied.add(alias)
        projections.append(f"r.{quote_identifier(column)} AS {quote_identifier(alias)}")
    return ", ".join(projections)


def execute_exact_snapshot_join(
    state: Any,
    join_plan: list[dict[str, Any]],
    *,
    max_rows: int = 250_000,
) -> dict[str, Any]:
    """Execute a confirmed exact join plan in the session's DuckDB kernel.

    Postgres participates only through its already-loaded session snapshot. No
    connector is implicitly scanned or copied in full by this operation.
    """
    contexts = state.get_all_data_contexts()
    if len(contexts) < 2:
        return {"error": "At least 2 datasets are required"}
    if not join_plan:
        return {"error": "join_plan is empty — at least one join step is required."}

    names = [str(context.filename) for context in contexts]
    if len(set(names)) != len(names):
        return {"error": "Dataset filenames must be unique before they can be joined."}

    for context in contexts:
        source = str((getattr(context, "metadata", None) or {}).get("source") or "upload")
        remote = callable(getattr(context, "is_remote_source", None)) and context.is_remote_source()
        if remote or source in SOURCE_WORKING_COPY_IDS:
            return {"error": CROSS_SOURCE_UNAVAILABLE_MESSAGE}

    planes = [resolve_data_plane(state, context, snapshot_postgres=True) for context in contexts]
    if any(plane.kind != PlaneKind.DUCKDB for plane in planes):
        return {"error": "Every exact-join input must resolve to a DuckDB snapshot."}
    kernel = planes[0].kernel
    if kernel is None or any(plane.kernel is not kernel for plane in planes):
        return {"error": "Join inputs did not resolve to the same session engine."}

    relations = {name: plane.relation_name for name, plane in zip(names, planes)}
    step_log: list[dict[str, Any]] = []
    result_table: pa.Table | None = None
    temporary_relations: list[str] = []
    joined_sources: set[str] = set()

    def source_name(ref: Any) -> str | None:
        if isinstance(ref, int) and 0 <= ref < len(names):
            return names[ref]
        return str(ref) if str(ref) in relations and not str(ref).startswith("_result_") else None

    try:
        for index, step in enumerate(join_plan):
            if str(step.get("strategy") or "exact").lower() != "exact":
                return {"error": f"Step {index}: only exact joins run in DuckDB."}
            left_ref = step.get("left")
            if left_ref is None:
                left_ref = names[0] if index == 0 else f"_result_{index - 1}"
            right_ref = step.get("right")
            if right_ref is None:
                if index + 1 >= len(names):
                    return {"error": f"Step {index}: no right dataset was specified."}
                right_ref = names[index + 1]

            logical_left_ref, logical_right_ref = left_ref, right_ref
            left_name, right_name = source_name(left_ref), source_name(right_ref)
            if index == 0:
                if left_name is not None:
                    joined_sources.add(left_name)
                if right_name is not None:
                    joined_sources.add(right_name)
            else:
                prior_result = f"_result_{index - 1}"
                left_joined = left_ref == prior_result or left_name in joined_sources
                right_joined = right_ref == prior_result or right_name in joined_sources
                left_new = left_name is not None and left_name not in joined_sources
                right_new = right_name is not None and right_name not in joined_sources
                if left_joined and right_new:
                    left_ref = prior_result
                    joined_sources.add(right_name)
                elif right_joined and left_new:
                    right_ref = prior_result
                    joined_sources.add(left_name)
                else:
                    return {
                        "error": (
                            f"Step {index}: each step must add exactly one new dataset "
                            "to the prior result."
                        )
                    }

            left_relation = _resolve_ref(left_ref, names, relations)
            right_relation = _resolve_ref(right_ref, names, relations)
            if left_relation is None:
                return {"error": f"Step {index}: cannot resolve left dataset '{left_ref}'"}
            if right_relation is None:
                return {"error": f"Step {index}: cannot resolve right dataset '{right_ref}'"}

            left_columns = _column_names(kernel, left_relation)
            right_columns = _column_names(kernel, right_relation)
            raw_pairs = step.get("keys") or []
            key_pairs = [
                (str(pair.get("left") or ""), str(pair.get("right") or "")) for pair in raw_pairs
            ]
            if not key_pairs:
                left_on = str(step.get("left_on") or "")
                right_on = str(step.get("right_on") or "")
                if left_on and right_on:
                    key_pairs = [(left_on, right_on)]
            if not key_pairs:
                common = next((name for name in left_columns if name in right_columns), None)
                if common is None:
                    return {
                        "error": (
                            f"Step {index}: no matching column names between '{left_ref}' "
                            f"and '{right_ref}'. Specify left_on/right_on."
                        )
                    }
                key_pairs = [(common, common)]
            if any(not left or not right for left, right in key_pairs):
                return {"error": f"Step {index}: every join key pair must name both columns."}
            for left_on, right_on in key_pairs:
                if left_on not in left_columns:
                    return {"error": f"Step {index}: left key '{left_on}' does not exist."}
                if right_on not in right_columns:
                    return {"error": f"Step {index}: right key '{right_on}' does not exist."}

            requested_type = str(step.get("join_type") or "INNER").upper()
            join_type = _JOIN_TYPES.get(requested_type)
            if join_type is None:
                return {"error": f"Step {index}: unsupported join type '{requested_type}'."}
            projection = _join_projection(left_columns, right_columns, key_pairs)
            left_order = "__stych_join_left_order"
            while left_order in left_columns:
                left_order += "_"
            right_order = "__stych_join_right_order"
            while right_order in right_columns or right_order == left_order:
                right_order += "_"
            ordering = (
                f"r.{quote_identifier(right_order)}, l.{quote_identifier(left_order)}"
                if join_type == "RIGHT"
                else f"l.{quote_identifier(left_order)}, r.{quote_identifier(right_order)}"
            )
            conditions = " AND ".join(
                f"l.{quote_identifier(left)} = r.{quote_identifier(right)}"
                for left, right in key_pairs
            )
            sql = (
                f"WITH left_source AS (SELECT *, row_number() OVER () AS "
                f"{quote_identifier(left_order)} FROM {quote_identifier(left_relation)}), "
                f"right_source AS (SELECT *, row_number() OVER () AS "
                f"{quote_identifier(right_order)} FROM {quote_identifier(right_relation)}) "
                f"SELECT {projection} FROM left_source AS l "
                f"{join_type} JOIN right_source AS r "
                f"ON {conditions} "
                f"ORDER BY {ordering} NULLS LAST"
            )
            result = kernel.query(sql, max_rows=max_rows)
            if result.truncated:
                return {
                    "error": (
                        f"Step {index}: join exceeded the {max_rows:,}-row safety limit. "
                        "Narrow the inputs or join keys before retrying."
                    )
                }
            result_table = result.table
            result_name = validate_relation_name(
                f"join_{index}_{hashlib.sha256(sql.encode()).hexdigest()[:8]}_{uuid.uuid4().hex[:8]}"
            )
            kernel.materialize(result_name, result_table, source="exact_join")
            temporary_relations.append(result_name)
            relations[f"_result_{index}"] = result_name
            step_log.append(
                {
                    "step": index,
                    "left": str(logical_left_ref),
                    "right": str(logical_right_ref),
                    "strategy": "exact",
                    "join_type": requested_type,
                    "left_on": key_pairs[0][0] if len(key_pairs) == 1 else None,
                    "right_on": key_pairs[0][1] if len(key_pairs) == 1 else None,
                    "keys": [{"left": left, "right": right} for left, right in key_pairs],
                    "result_rows": result.row_count,
                    "engine": "duckdb",
                }
            )
    except Exception as exc:  # keep the existing linking-result error contract
        return {"error": str(exc)}
    finally:
        for relation_name in temporary_relations:
            kernel.remove_relation(relation_name)

    assert result_table is not None
    result_frame = result_table.to_pandas()
    return {
        "data": result_frame.to_dict(orient="list"),
        "rows": len(result_frame),
        "columns": list(result_frame.columns),
        "datasets_count": len(contexts),
        "steps_executed": len(step_log),
        "step_log": step_log,
        "original_shapes": {
            name: [len(context.working_dataframe), len(context.working_dataframe.columns)]
            for name, context in zip(names, contexts)
        },
        "engine": "duckdb",
        "input_scopes": {
            name: {
                "connector": plane.connector_id,
                "scope": plane.scope.value,
                "rows": plane.row_count,
                "truncated": plane.truncated,
            }
            for name, plane in zip(names, planes)
        },
    }

"""Execute a join inside the customer's own engine.

Both sides stay where they live: the engine reads the full relations, and only
the *result* is written — into the Stych-owned scratch schema, registered as a
working copy so preview, export, diff, and further joins all treat it like any
other relation. No row of either input passes through this process.

Only exact-equality joins take this path. Similarity matching is routed by
``linking.strategy_support``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

import pandas as pd

from agents.sql_agent import working_copies as wc
from agents.sql_agent.protocols import ScratchSQLRunner, runner_for
from agents.sql_agent.runner import dataset_table_prefix
from datasources.join_dialect import get_join_dialect
from datasources.join_planner import JoinPlan, JoinRelation
from datasources.warehouse import WarehouseError, validate_read_only_query

logger = logging.getLogger(__name__)

# Rows pulled back for the UI grid and the agent's summary. The join result
# itself stays in the source; this is only what we show.
PREVIEW_ROWS = 500

_VALID_JOIN_TYPES = {"INNER", "LEFT", "RIGHT", "FULL OUTER"}
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .\-]*$")


class SourceJoinError(Exception):
    """The join cannot be executed inside the source."""


@dataclass
class SourceJoinResult:
    table: str
    schema: str
    rows: int
    columns: List[str]
    preview: pd.DataFrame
    sql: str
    step_log: List[Dict[str, Any]] = field(default_factory=list)


def _quote(name: str, dialect: str) -> str:
    return get_join_dialect(dialect).quote_identifier(str(name))


def _dialect_of(ctx: Any) -> str:
    return str((getattr(ctx, "metadata", None) or {}).get("source") or "postgres")


def _validate_column(name: str) -> str:
    """Column names reach SQL as quoted identifiers; reject anything that a
    quote could not contain safely rather than trusting LLM-proposed keys."""
    text = str(name or "").strip()
    if not text or len(text) > 128 or "\x00" in text:
        raise SourceJoinError(f"Invalid join column {name!r}.")
    return text


def _relation_sql(
    relation: JoinRelation,
    alias: str,
    dialect: str,
    runner: ScratchSQLRunner | None = None,
) -> str:
    if relation.is_physical:
        if relation.database:
            ref = get_join_dialect(dialect).quote_relation(
                str(relation.schema),
                str(relation.table),
                database=relation.database,
            )
        elif runner is not None:
            ref = runner.quote_relation(str(relation.schema), str(relation.table))
        else:
            ref = get_join_dialect(dialect).quote_relation(
                str(relation.schema), str(relation.table)
            )
    elif relation.custom_query:
        try:
            query = validate_read_only_query(relation.custom_query)
        except WarehouseError as exc:
            raise SourceJoinError(
                f"Dataset {relation.name!r} has an unsafe custom query: {exc}"
            ) from exc
        ref = f"({query})"
    else:
        raise SourceJoinError(f"Dataset {relation.name!r} has no readable relation.")
    return f"{ref} AS {_quote(alias, dialect)}"


def _relation_columns(runner: ScratchSQLRunner, relation: JoinRelation, dialect: str) -> List[str]:
    if relation.is_physical:
        # The Databricks runner introspects its configured scratch catalog.
        # Same-workspace joins may legitimately span Unity Catalog catalogs,
        # so use the attached relation's complete catalog schema for a relation
        # outside that runner catalog instead of querying the wrong catalog.
        runner_catalog = getattr(runner, "scratch_catalog", None)
        if dialect == "databricks" and relation.database and relation.database != runner_catalog:
            frame = getattr(relation.ctx, "dataframe", None)
            if frame is None:
                raise SourceJoinError(f"Cannot resolve columns for {relation.name!r}.")
            return [str(column) for column in frame.columns]
        columns = runner.get_table_columns(relation.schema, relation.table)
        return [str(c.get("column_name") or c.get("name")) for c in columns]
    frame = getattr(relation.ctx, "dataframe", None)
    if frame is None:
        raise SourceJoinError(f"Cannot resolve columns for {relation.name!r}.")
    return [str(c) for c in frame.columns]


def _select_list(
    per_relation: Sequence[tuple[str, List[str]]], dialect: str
) -> tuple[str, List[str]]:
    """Project every column once, suffixing later duplicates so a joined result
    never silently drops a same-named column from the right-hand side."""
    seen: Dict[str, int] = {}
    parts: List[str] = []
    output: List[str] = []
    for alias, columns in per_relation:
        for column in columns:
            label = column
            if label in seen:
                seen[label] += 1
                label = f"{column}_{alias}"
                while label in seen:
                    seen[label] = seen.get(label, 0) + 1
                    label = f"{label}_x"
            seen.setdefault(label, 0)
            seen[column] = seen.get(column, 0)
            parts.append(
                f"{_quote(alias, dialect)}.{_quote(column, dialect)} AS {_quote(label, dialect)}"
            )
            output.append(label)
    return ", ".join(parts), output


def _relation_index(ref: Any, relations: Sequence[JoinRelation]) -> int:
    if isinstance(ref, int) and 0 <= ref < len(relations):
        return ref
    text = str(ref)
    for index, relation in enumerate(relations):
        if relation.name == text:
            return index
    raise SourceJoinError(f"Cannot resolve join dataset {ref!r}.")


def build_join_sql(
    relations: Sequence[JoinRelation],
    steps: Sequence[Dict[str, Any]],
    column_map: Dict[int, List[str]],
    dialect: str,
    runner: ScratchSQLRunner | None = None,
) -> tuple[str, List[str]]:
    """One SELECT chaining every step. Returns (sql, output column labels)."""
    if len(relations) < 2:
        raise SourceJoinError("A join needs at least two datasets.")
    aliases = [f"t{i}" for i in range(len(relations))]
    select_sql, labels = _select_list(
        [(aliases[i], column_map[i]) for i in range(len(relations))], dialect
    )
    first_index = _relation_index(steps[0]["left"], relations) if steps else 0
    clauses = [
        f"FROM {_relation_sql(relations[first_index], aliases[first_index], dialect, runner)}"
    ]
    joined = {first_index}
    for step in steps:
        left_idx = _relation_index(step["left"], relations)
        right_idx = _relation_index(step["right"], relations)
        swapped = False
        if right_idx in joined:
            left_idx, right_idx = right_idx, left_idx
            swapped = True
        if left_idx not in joined:
            raise SourceJoinError("Join steps must chain from an already-joined dataset.")
        join_type = str(step.get("join_type") or "INNER").upper().strip()
        if join_type not in _VALID_JOIN_TYPES:
            raise SourceJoinError(f"Unsupported join type {join_type!r}.")
        if swapped:
            join_type = {"LEFT": "RIGHT", "RIGHT": "LEFT"}.get(join_type, join_type)
        pairs = step.get("keys") or [{"left": step.get("left_on"), "right": step.get("right_on")}]
        conditions = []
        for pair in pairs:
            left_col = _validate_column(pair.get("right") if swapped else pair.get("left"))
            right_col = _validate_column(pair.get("left") if swapped else pair.get("right"))
            conditions.append(
                f"{_quote(aliases[left_idx], dialect)}.{_quote(left_col, dialect)} = "
                f"{_quote(aliases[right_idx], dialect)}.{_quote(right_col, dialect)}"
            )
        clauses.append(
            f"{join_type} JOIN "
            f"{_relation_sql(relations[right_idx], aliases[right_idx], dialect, runner)} "
            f"ON {' AND '.join(conditions)}"
        )
        joined.add(right_idx)
    if len(joined) != len(relations):
        missing = [relations[i].name for i in range(len(relations)) if i not in joined]
        raise SourceJoinError(f"No join step connects: {', '.join(missing)}.")
    return f"SELECT {select_sql} " + " ".join(clauses), labels


def execute_source_join(
    plan: JoinPlan,
    steps: Sequence[Dict[str, Any]],
    *,
    session_id: str,
    state: Any,
) -> SourceJoinResult:
    """Run the join in the source and leave the result in the scratch schema."""
    if not plan.source_side:
        raise SourceJoinError("This join is not eligible for source-side execution.")
    relations = list(plan.relations)
    anchor = relations[0].ctx
    dialect = _dialect_of(anchor)
    runner = runner_for(anchor.metadata)
    join_dialect = get_join_dialect(dialect)
    meta = anchor.metadata
    wc.adopt_legacy(meta)
    if len(wc.list_copies(meta)) >= wc.MAX_COPIES:
        raise SourceJoinError(
            f"Working-copy limit reached ({wc.MAX_COPIES}). Drop a copy before joining."
        )
    copy_name = wc.default_name(meta)

    column_map = {i: _relation_columns(runner, rel, dialect) for i, rel in enumerate(relations)}
    select_sql, labels = build_join_sql(relations, steps, column_map, dialect, runner=runner)

    scratch = runner.temp_schema
    wc.bind_scratch_schema(meta, scratch)
    table = (
        f"{dataset_table_prefix(session_id, getattr(anchor, 'file_id', 'join'))}"
        f"join_{uuid.uuid4().hex[:8]}"
    )
    target = runner.quote_relation(scratch, table)

    created = False
    try:
        runner.ensure_temp_schema()
        runner.execute_statements_transaction(
            [join_dialect.materialize_join(target, select_sql)], allowed_schema=scratch
        )
        created = True
        rows = runner.get_row_count(scratch, table)
        preview = runner.run_sql_read_only(join_dialect.preview_relation(target, PREVIEW_ROWS))
    except Exception as exc:  # noqa: BLE001
        if created:
            try:
                runner.drop_temp_table(table)
            except Exception:  # noqa: BLE001
                logger.warning("[linking] could not clean up failed join result %s", table)
        raise SourceJoinError(str(exc)) from exc

    try:
        wc.register_copy(meta, name=copy_name, table=table, parent=wc.SOURCE_PARENT)
    except wc.WorkingCopyError as exc:
        try:
            runner.drop_temp_table(table)
        except Exception:  # noqa: BLE001
            logger.warning("[linking] could not clean up unregistered join result %s", table)
        raise SourceJoinError(f"Could not register the join working copy: {exc}") from exc

    step_log = [
        {
            "left": relations[_relation_index(s["left"], relations)].name,
            "right": relations[_relation_index(s["right"], relations)].name,
            "join_type": str(s.get("join_type") or "INNER").upper(),
            "strategy": "exact",
            "executed_in": dialect,
        }
        for s in steps
    ]
    return SourceJoinResult(
        table=table,
        schema=scratch,
        rows=rows,
        columns=labels,
        preview=preview,
        sql=select_sql,
        step_log=step_log,
    )

"""
SQL-engine annotation materialization.

Annotate steps become LEFT-JOINable relations against the source table:

- ``duplicate_values`` — never materialized; compiled to an inline GROUP BY /
  HAVING derived table (exact, full-table pushdown).
- ``fuzzy_duplicates`` — computed by the pandas builtin over a bounded
  ``fetch_sample`` and written to ``stych_temp."flt_<fp8>_<step_id>"``
  (a derived annotation table, not user data).
- ``sql`` — the agent's validated SELECT materialized the same way.
- ``pandas`` — skipped on database sources (file-engine only).

A tiny registry keyed on (session, source identity incl. temp_table_generation,
step fingerprint) reuses tables until the working table mutates — a generation
bump misses the key and the stale table is dropped and rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from datasources.sql_dialect import get_dialect
from datasources.sql_readonly import ReadOnlySQLError, parse_read_only_statements
from filters import builtins as fb
from filters.spec import (
    DuplicateValuesStep,
    FilterSpec,
    FuzzyDuplicatesStep,
    PandasStep,
    SqlStep,
)

logger = logging.getLogger(__name__)

TEMP_SCHEMA = "stych_temp"
ANNOT_KEY = "__row_key__"
_REGISTRY_MAX = 16

# (session_id, *adapter.cache_info.key, step_fingerprint) -> temp table name
_annot_registry: "OrderedDict[Tuple, str]" = OrderedDict()


@dataclass
class AnnotationJoin:
    """One LEFT JOIN + the output expressions it contributes."""

    step_id: str
    alias: str
    join_sql: str
    outputs: Dict[str, str] = field(default_factory=dict)  # output name -> SQL expr


def _quote(name: str, dialect: str = "postgres") -> str:
    """Quote an identifier for SQLAlchemy fallback paths."""
    return get_dialect(dialect).quote(name)


def _status(step_id: str, status: str, **extra: Any) -> Dict[str, Any]:
    return {
        "step_id": step_id,
        "status": status,
        "rows": extra.pop("rows", 0),
        "coverage": extra.pop("coverage", 0.0),
        "error": extra.pop("error", None),
        "cached": extra.pop("cached", False),
    }


def _step_fingerprint(step) -> str:
    canon = json.dumps(step.model_dump(), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canon.encode()).hexdigest()[:8]


def _engine(adapter):
    from agents.sql_agent.runner import get_shared_engine  # noqa: PLC0415

    return get_shared_engine(adapter.connection_url)


def _runner(adapter):
    """Scratch runner for this adapter's engine, or None when it has none.

    The adapter carries the connection; ``dialect`` doubles as the connector id
    for every source-side engine, so the shared factory can resolve the runner
    without the caller threading a DataContext down here.
    """
    from agents.sql_agent.protocols import runner_for, supports_source_execution  # noqa: PLC0415
    from datasources.warehouse import encode_connection_config  # noqa: PLC0415

    meta = {"source": str(getattr(adapter, "dialect", "") or "")}
    if not supports_source_execution(meta):
        return None
    if getattr(adapter, "connection_url", None):
        meta["connection_url"] = adapter.connection_url
    config = getattr(adapter, "connection_config", None)
    if config:
        meta["connection_config"] = encode_connection_config(config)
        # BigQuery co-locates its scratch dataset with the source dataset.
        meta["schema"] = getattr(adapter, "db_schema", None)
    try:
        return runner_for(meta)
    except Exception as exc:  # noqa: BLE001 — an unusable runner means "cannot materialize"
        logger.info("no scratch runner for %s: %s", meta.get("source"), exc)
        return None


def _scratch_schema(adapter) -> str:
    runner = _runner(adapter)
    return runner.temp_schema if runner is not None else TEMP_SCHEMA


def _scratch_ref(adapter, table: str) -> str:
    """Fully-qualified annotation table, three-part where the engine needs it."""
    runner = _runner(adapter)
    if runner is not None:
        return runner.quote_relation(runner.temp_schema, table)
    dialect = getattr(adapter, "dialect", "postgres") or "postgres"
    return f"{_quote(TEMP_SCHEMA, dialect)}.{_quote(table, dialect)}"


def _drop_table(adapter, table: str) -> None:
    try:
        runner = _runner(adapter)
        if runner is not None:
            runner.drop_temp_table(table)
            return
        from sqlalchemy import text  # noqa: PLC0415

        dialect = getattr(adapter, "dialect", "postgres") or "postgres"
        with _engine(adapter).begin() as conn:
            ref = f"{_quote(TEMP_SCHEMA, dialect)}.{_quote(table, dialect)}"
            conn.execute(text(get_dialect(dialect).drop_table_if_exists(ref)))
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("could not drop stale annotation table %s", table, exc_info=True)


def _register(adapter, key: Tuple, table: str) -> None:
    _annot_registry[key] = table
    _annot_registry.move_to_end(key)
    while len(_annot_registry) > _REGISTRY_MAX:
        _evicted_key, evicted = _annot_registry.popitem(last=False)
        _drop_table(adapter, evicted)


def duplicate_values_join(
    step: DuplicateValuesStep,
    source_ref: str,
    alias: str,
    *,
    quote: str = '"',
    dialect: str = "postgres",
) -> AnnotationJoin:
    """Inline exact-duplicate grouping: full pushdown, no materialization.

    Null-key rows are excluded in the subquery (a shared missing value is not
    a duplicate), so a plain equality join suffices.
    """

    def q(name: str) -> str:
        if quote == "[":
            return "[" + str(name).replace("]", "]]") + "]"
        return quote + str(name).replace(quote, quote + quote) + quote

    group_cols = [q(c) for c in step.columns]
    sel = ", ".join(f"{c} AS g{i}" for i, c in enumerate(group_cols))
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in group_cols)
    count_out, group_out = step.outputs[0].name, step.outputs[1].name
    key_cast = get_dialect(dialect).cast_text(group_cols[0])
    sub = (
        f"SELECT {sel}, COUNT(*) AS {q(count_out)}, "
        f"MIN({key_cast}) AS {q(group_out)} "
        f"FROM {source_ref} WHERE {not_null} "
        f"GROUP BY {', '.join(group_cols)} HAVING COUNT(*) >= {int(step.min_count)}"
    )
    on = " AND ".join(f"{alias}.g{i} = t.{c}" for i, c in enumerate(group_cols))
    return AnnotationJoin(
        step_id=step.id,
        alias=alias,
        join_sql=f"LEFT JOIN ({sub}) {alias} ON {on}",
        outputs={
            count_out: f"{alias}.{q(count_out)}",
            group_out: f"{alias}.{q(group_out)}",
        },
    )


def _table_join(
    adapter, step, alias: str, table: str, key_column: str, source_key: str
) -> AnnotationJoin:
    from datasources.sql_dialect import get_dialect  # noqa: PLC0415

    dialect = get_dialect(getattr(adapter, "dialect", None))
    left = dialect.cast_text(f"{alias}.{dialect.quote(key_column)}")
    right = dialect.cast_text(f"t.{dialect.quote(source_key)}")
    return AnnotationJoin(
        step_id=step.id,
        alias=alias,
        join_sql=f"LEFT JOIN {_scratch_ref(adapter, table)} {alias} ON {left} = {right}",
        outputs={o.name: f"{alias}.{dialect.quote(o.name)}" for o in step.outputs},
    )


def _can_materialize(adapter) -> bool:
    """True when a SELECT step can be written to a scratch table in the source."""
    return _runner(adapter) is not None or bool(getattr(adapter, "connection_url", None))


def _can_compute_fuzzy(adapter) -> bool:
    """Fuzzy grouping runs in Python and writes a frame back through SQLAlchemy.

    It is also inherently sampled — the union-find needs every value in memory —
    so it stays off the engines that would otherwise present it as full-data.
    """
    return bool(getattr(adapter, "connection_url", None))


def _materialize_fuzzy(
    adapter, step: FuzzyDuplicatesStep, source_key: str, table: str
) -> Tuple[int, bool]:
    """Sample → pandas builtin → write annotation table. Returns (rows, degraded)."""
    import pandas as pd  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    total = adapter.count(None, None).value
    degraded = total > step.max_rows
    sample = adapter.fetch_sample(columns=[source_key, step.on], limit=step.max_rows)
    values = pd.Series(sample[step.on].to_numpy(), index=pd.Index(sample[source_key]))
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
    frame = frame.reset_index(names=ANNOT_KEY)
    frame[ANNOT_KEY] = frame[ANNOT_KEY].astype(str)

    dialect_name = getattr(adapter, "dialect", "postgres") or "postgres"
    runner = _runner(adapter)
    engine = _engine(adapter)
    schema = runner.temp_schema if runner is not None else TEMP_SCHEMA
    with engine.begin() as conn:
        if runner is None:
            d = get_dialect(dialect_name)
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_quote(schema, dialect_name)}"))
            ref = f"{_quote(schema, dialect_name)}.{_quote(table, dialect_name)}"
            conn.execute(text(d.drop_table_if_exists(ref)))
    if runner is not None:
        runner.ensure_temp_schema()
        # pandas uses SQLAlchemy's dialect-aware CREATE TABLE/INSERT path; the
        # runner owns schema creation and cleanup for SQL Server scratch data.
        runner.drop_temp_table(table)
    frame.to_sql(table, engine, schema=schema, if_exists="replace", index=False)
    return len(frame), degraded


def _materialize_sql(adapter, step: SqlStep, table: str) -> int:
    """Run the step's SELECT into a scratch table, inside the source engine.

    Nothing is sampled and no rows leave the database: the whole step is one
    CREATE TABLE AS against the full relation.
    """
    # Use the shared dialect-agnostic read-only validator instead of a
    # connector-specific wrapper so that Snowflake, BigQuery, Redshift,
    # Databricks and SQL Server all get correct handling (previously they
    # fell through to the Postgres validator branch).
    raw_sql = step.sql.replace("{table}", adapter.source_ref)
    try:
        stmts = parse_read_only_statements(raw_sql, allowed_starts=frozenset({"SELECT", "WITH"}))
    except ReadOnlySQLError as exc:
        raise ValueError(f"SqlStep SQL must be a read-only SELECT statement: {exc}") from exc
    if len(stmts) != 1:
        raise ValueError("SqlStep SQL must be a single SELECT statement")
    select = stmts[0]
    ref = _scratch_ref(adapter, table)
    runner = _runner(adapter)
    dialect_name = getattr(adapter, "dialect", "postgres") or "postgres"
    d = get_dialect(dialect_name)
    if runner is not None:
        runner.ensure_temp_schema()
        # The runner's drop helper owns dialect-specific conditional DDL and
        # bypasses the write guard's statement-shape restrictions.
        runner.drop_temp_table(table)
        runner.execute_statements_transaction(
            [d.create_table_as(ref, "*", f"({select}) AS _src")],
            allowed_schema=runner.temp_schema,
        )
        count = runner.get_row_count(runner.temp_schema, table)
    else:
        from sqlalchemy import text  # noqa: PLC0415

        engine = _engine(adapter)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_quote(TEMP_SCHEMA, dialect_name)}"))
            conn.execute(text(d.drop_table_if_exists(ref)))
            conn.execute(text(d.create_table_as(ref, "*", f"({select}) AS _src")))
            count = conn.execute(text(f"SELECT COUNT(*) FROM {ref}")).scalar()
    # Declared outputs + key must exist; a missing column should fail here,
    # not as an opaque error in the page query.
    probe = f"SELECT {d.select_prefix(0)}* FROM {ref}{d.limit_suffix(0)}"
    keys, _rows = adapter.run(probe)
    missing = [c for c in [step.key_column, *(o.name for o in step.outputs)] if c not in keys]
    if missing:
        _drop_table(adapter, table)
        raise ValueError(f"annotation SELECT is missing columns: {missing}")
    return int(count or 0)


def build_annotation_joins(
    adapter,
    spec: FilterSpec,
    *,
    session_id: str,
    source_key: Optional[str],
) -> Tuple[List[AnnotationJoin], List[Dict[str, Any]], Optional[str]]:
    """Materialize/compile every annotate step. Returns (joins, statuses,
    degraded). With no usable row key, table-backed steps are skipped
    (``degraded="no_key_annotations"``); inline duplicate_values still works."""
    joins: List[AnnotationJoin] = []
    statuses: List[Dict[str, Any]] = []
    degraded: Optional[str] = None

    for idx, step in enumerate(spec.annotate):
        alias = f"a{idx}"
        if isinstance(step, DuplicateValuesStep):
            joins.append(
                duplicate_values_join(
                    step,
                    adapter.source_ref,
                    alias,
                    quote=getattr(adapter, "ident_quote", None) or '"',
                    dialect=getattr(adapter, "dialect", "postgres"),
                )
            )
            statuses.append(_status(step.id, "ok"))
            continue
        if isinstance(step, PandasStep):
            statuses.append(_status(step.id, "skipped", error="pandas steps require a file source"))
            continue
        engine_name = getattr(adapter, "dialect", None) or "this source"
        if isinstance(step, FuzzyDuplicatesStep) and not _can_compute_fuzzy(adapter):
            statuses.append(
                _status(
                    step.id,
                    "skipped",
                    error=(
                        f"fuzzy-duplicate detection is not available on {engine_name}: it would "
                        "run on a sample of the table rather than all of it"
                    ),
                )
            )
            continue
        if not _can_materialize(adapter):
            statuses.append(
                _status(
                    step.id,
                    "skipped",
                    error=f"{engine_name} cannot materialize annotation steps",
                )
            )
            continue
        if source_key is None:
            statuses.append(
                _status(step.id, "skipped", error="no unique key column — cannot join annotations")
            )
            degraded = "no_key_annotations"
            continue

        fingerprint = _step_fingerprint(step)
        cache_key = (session_id, *adapter.cache_info.key, fingerprint)
        table = f"flt_{fingerprint}_{step.id}"[:63]
        cached = _annot_registry.get(cache_key) == table
        try:
            if isinstance(step, FuzzyDuplicatesStep):
                if cached:
                    rows, capped = None, False
                else:
                    rows, capped = _materialize_fuzzy(adapter, step, source_key, table)
                if capped:
                    degraded = degraded or "fuzzy_row_cap"
                joins.append(_table_join(adapter, step, alias, table, ANNOT_KEY, source_key))
                statuses.append(_status(step.id, "ok", rows=rows or 0, cached=cached))
            elif isinstance(step, SqlStep):
                rows = None if cached else _materialize_sql(adapter, step, table)
                joins.append(_table_join(adapter, step, alias, table, step.key_column, source_key))
                statuses.append(_status(step.id, "ok", rows=rows or 0, cached=cached))
            else:  # pragma: no cover — closed union
                statuses.append(_status(step.id, "error", error="unknown step kind"))
                continue
            _register(adapter, cache_key, table)
        except Exception as exc:  # noqa: BLE001 — a failed step must not kill the page
            logger.exception("annotation step %s failed", step.id)
            statuses.append(_status(step.id, "error", error=str(exc)[:400]))

    return joins, statuses, degraded


def clear_registry() -> None:
    """Test helper."""
    _annot_registry.clear()

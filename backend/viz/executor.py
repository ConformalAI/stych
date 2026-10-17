"""
Executor — validate a VizSpec and materialize its data on the right engine.

The single entry point every consumer uses (REST query route, agent tools,
persisted-viz refresh): normalize → validate against the field catalog →
build the shared QueryPlan → execute in the session DuckDB snapshot or push
SQL down to the Postgres session table.
"""

from __future__ import annotations

import asyncio
import logging
from copy import copy
from typing import Any, List, Optional

from .catalog import FieldCatalog, get_field_catalog
from .compile_sql import compile_spec_duckdb, compile_spec_sql, extract_hidden_meta
from .expr import ResolvedCalc, extend_catalog, resolve_calculations
from .plan import VizData, build_plan, plan_columns
from .shaping import _json_value
from .spec import CalculateTransform, DataRef, SpecError, VizSpec
from .validate import normalize_spec, validate_spec

logger = logging.getLogger(__name__)


class SpecValidationError(Exception):
    """Raised when a spec fails semantic validation; carries structured errors."""

    def __init__(self, errors: List[SpecError]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors

    def to_payload(self) -> List[dict]:
        return [e.model_dump() for e in self.errors]


class SourceUnavailableError(ValueError):
    """The dataset named by a visualization no longer exists in its session."""

    def __init__(self, source: str):
        self.source = source
        super().__init__(
            f"This visualization's data source ({source}) is no longer available in this session."
        )


def _all_data_contexts(state: Any) -> List[Any]:
    getter = getattr(state, "get_all_data_contexts", None)
    if callable(getter):
        return list(getter())
    primary = getattr(state, "data_context", None)
    extras = list(getattr(state, "additional_data_contexts", []) or [])
    return ([primary] if primary is not None else []) + extras


def resolve_data_context(state: Any, source: str, source_hint: Optional[str] = None) -> Any:
    """Resolve a spec source to one DataContext without changing session focus.

    ``active`` is accepted for unsaved/legacy specs. A persisted legacy row can
    supply its old filename through ``source_hint`` so promotion of another
    dataset never makes the visualization silently read the wrong data.
    """
    contexts = _all_data_contexts(state)
    primary = getattr(state, "data_context", None)
    requested = str(source or "active")

    if requested == "active":
        if source_hint:
            hint = str(source_hint)
            for dc in contexts:
                if (
                    str(getattr(dc, "file_id", "")) == hint
                    or str(getattr(dc, "filename", "")) == hint
                ):
                    return dc
            raise SourceUnavailableError(hint)
        if primary is None:
            raise SourceUnavailableError("active")
        return primary

    for dc in contexts:
        if str(getattr(dc, "file_id", "")) == requested:
            return dc
    raise SourceUnavailableError(requested)


def _state_for_context(state: Any, data_context: Any) -> Any:
    if getattr(state, "data_context", None) is data_context:
        return state
    scoped = copy(state)
    scoped.data_context = data_context
    return scoped


def _resolve_spec(
    spec: VizSpec, catalog: FieldCatalog
) -> tuple[VizSpec, FieldCatalog, List[ResolvedCalc], List[SpecError]]:
    """Resolve calculated columns, then normalize + validate against the
    effective catalog (dataset fields + calculated fields)."""
    calc_indices = [
        i for i, transform in enumerate(spec.transform) if isinstance(transform, CalculateTransform)
    ]
    calcs, calc_errors = resolve_calculations(
        spec.calculations(), catalog, transform_indices=calc_indices
    )
    effective = extend_catalog(catalog, calcs)
    spec = normalize_spec(spec, effective)
    errors = calc_errors + validate_spec(spec, effective)
    return spec, effective, calcs, errors


async def materialize_spec(
    state: Any,
    spec: VizSpec,
    sql_agent: Any = None,
    catalog: Optional[FieldCatalog] = None,
    source_hint: Optional[str] = None,
) -> tuple[VizSpec, VizData]:
    """Resolve + validate the spec ONCE, then execute it.

    Returns ``(normalized_spec, data)`` so callers that need both the canonical
    spec (to persist) and its data (to render) don't re-resolve/re-validate — and,
    on SQL sessions, don't re-issue the catalog's column/row-count probes — a
    second time. ``run_spec`` is the data-only wrapper for callers that don't need
    the normalized spec back.
    """
    dc = resolve_data_context(state, spec.data.source, source_hint=source_hint)
    scoped_state = _state_for_context(state, dc)
    # Canonical specs always carry a stable source id, including specs submitted
    # as ``active`` by older clients and agent prompts.
    spec = spec.model_copy(update={"data": DataRef(source=str(dc.file_id))})
    if catalog is None:
        catalog = await get_field_catalog(scoped_state, sql_agent)

    # A channel may name a certified metric instead of a field+aggregate. Expand
    # it into ordinary VizSpec constructs BEFORE resolution, so everything below
    # this line — calculations, validation, planning, both compilers — is
    # unchanged. Specs that name no metric skip this entirely, which is what
    # keeps the ontology optional rather than a precondition for charting.
    from ontology.viz_bridge import (  # noqa: PLC0415 — ontology imports viz
        expand_metric_channels,
        load_session_ontology,
        spec_uses_metrics,
    )

    if spec_uses_metrics(spec):
        spec, metric_errors = expand_metric_channels(
            spec, load_session_ontology(scoped_state), str(dc.file_id)
        )
        if metric_errors:
            raise SpecValidationError(metric_errors)

    normalized, effective, calcs, errors = _resolve_spec(spec, catalog)
    if errors:
        raise SpecValidationError(errors)

    plan = build_plan(normalized, calcs=calcs)
    from execution.session_plane import PlaneKind, resolve_data_plane  # noqa: PLC0415

    plane = await asyncio.to_thread(resolve_data_plane, scoped_state, dc)
    if plane.kind == PlaneKind.SOURCE and plane.connector_id == "mongodb":
        # Documents are charted by aggregation; there is no SQL to push down.
        from agents.mongo_agent import MongoScratchRunner  # noqa: PLC0415

        from .execute_mongo import mongo_target, run_mongo_plan  # noqa: PLC0415

        meta_for_mongo = dc.metadata or {}
        database, collection = mongo_target(meta_for_mongo)
        if not database or not collection:
            raise ValueError("MongoDB session has no resolvable collection target.")
        records = await asyncio.to_thread(
            run_mongo_plan,
            MongoScratchRunner.from_meta(meta_for_mongo),
            plan,
            effective,
            database=database,
            collection=collection,
        )
        return normalized, sql_records_to_vizdata(records, plan, engine="mongodb")

    if plane.kind == PlaneKind.SOURCE:
        if sql_agent is None:
            raise ValueError("SQL session requires sql_agent to execute a spec.")
        schema, table = dc.get_sql_target()
        if not schema or not table:
            raise ValueError("SQL session has no resolvable table target.")
        # Compile for the engine that will actually run this. Defaulting to
        # Postgres emitted "schema"."table", which BigQuery reads as a string
        # literal ('Unexpected string literal "stych_temp"').
        meta_for_sql = dc.metadata or {}
        compile_kw: dict = {"dialect": plane.connector_id}
        database = (
            meta_for_sql.get("scratch_catalog")
            or meta_for_sql.get("project_id")
            or meta_for_sql.get("database")
        )
        if database:
            compile_kw["database"] = database
        sql = compile_spec_sql(plan, effective, str(schema), str(table), **compile_kw)
        # DEBUG, not INFO: the compiled SQL inlines user filter values as literals.
        logger.debug("[viz] executing spec SQL: %s", sql)
        meta = dc.metadata or {}
        has_copy = bool(meta.get("temp_schema") and meta.get("temp_table"))
        records = await sql_agent.run_sql(scoped_state, sql, ensure_working_copy=has_copy)
        return normalized, sql_records_to_vizdata(records, plan)

    if plane.kernel is None:
        raise ValueError("DuckDB snapshot is unavailable for this dataset.")
    sql = compile_spec_duckdb(plan, effective, plane.relation_name)
    logger.debug("[viz] executing DuckDB spec SQL: %s", sql)
    result = await asyncio.to_thread(
        plane.kernel.query,
        sql,
        None,
        max_rows=plan.row_cap + 1,
    )
    data = sql_records_to_vizdata(result.table.to_pylist(), plan, engine="duckdb")
    data.meta.update(
        {
            "scope": plane.scope.value,
            "freshness": plane.freshness.value,
            "source_truncated": plane.truncated,
            "source_row_limit": plane.row_limit,
        }
    )
    return normalized, data


async def run_spec(
    state: Any,
    spec: VizSpec,
    sql_agent: Any = None,
    catalog: Optional[FieldCatalog] = None,
    source_hint: Optional[str] = None,
) -> VizData:
    """Materialize ``spec`` against its stable session dataset (data only)."""
    _, data = await materialize_spec(
        state,
        spec,
        sql_agent=sql_agent,
        catalog=catalog,
        source_hint=source_hint,
    )
    return data


def sql_records_to_vizdata(records: List[dict], plan: Any, *, engine: str = "sql") -> VizData:
    """Map ``run_sql`` records onto the tidy-table contract (shared with tests)."""
    truncated = len(records) > plan.row_cap
    records = records[: plan.row_cap]
    bins = extract_hidden_meta(records, plan)
    dims = {d.channel: d for d in plan.dims}
    channels = plan.channels()
    rows = [[_json_value(r.get(c), dims.get(c)) for c in channels] for r in records]
    meta: dict = {
        "row_count": len(rows),
        "truncated": truncated,
        "engine": engine,
    }
    if bins:
        meta["bins"] = bins
    return VizData(columns=plan_columns(plan), rows=rows, meta=meta)

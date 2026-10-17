"""
Executor — resolve an ontology query and materialize it.

The single entry point every consumer uses (REST route, agent tools). Mirrors
``viz.executor``: resolve → validate → build plan → execute on the right engine.

Engine selection follows the entities a query touches:

- **One entity** — delegate straight to the compilers the viz subsystem already
  parity-tests: pandas for file sessions, SQL pushdown for warehouse sessions.
- **Several entities** — materialize the join in pandas, then run the same plan
  against the joined frame. Cross-entity queries over warehouse sources are
  refused with a clear error rather than silently pulling whole tables into
  memory; single-entity metrics still push down.
"""

from __future__ import annotations

import asyncio
import logging
from copy import copy
from typing import Any, Dict, List, Tuple

import pandas as pd

from viz.catalog import FieldCatalog, build_catalog_pandas, get_field_catalog
from viz.compile_pandas import execute_pandas
from viz.compile_sql import compile_spec_sql
from viz.executor import SourceUnavailableError, sql_records_to_vizdata
from viz.plan import VizData

from .resolve import (
    JoinStep,
    QueryDimension,
    ResolutionError,
    ResolvedQuery,
    joined_column,
    plan_join,
    resolve_query,
)
from .spec import OntologyError, OntologySpec
from .validate import normalize_spec, validate_spec

logger = logging.getLogger(__name__)


class OntologyValidationError(Exception):
    """The spec or query failed validation; carries structured errors."""

    def __init__(self, errors: List[OntologyError]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors

    def to_payload(self) -> List[dict]:
        return [e.model_dump() for e in self.errors]


def _all_data_contexts(state: Any) -> List[Any]:
    getter = getattr(state, "get_all_data_contexts", None)
    if callable(getter):
        return list(getter())
    primary = getattr(state, "data_context", None)
    extras = list(getattr(state, "additional_data_contexts", []) or [])
    return ([primary] if primary is not None else []) + extras


def resolve_dataset(state: Any, dataset: str) -> Any:
    """Resolve a spec dataset id to its DataContext without changing session focus."""
    for dc in _all_data_contexts(state):
        if str(getattr(dc, "file_id", "")) == str(dataset):
            return dc
    raise SourceUnavailableError(str(dataset))


def _scoped_state(state: Any, data_context: Any) -> Any:
    if getattr(state, "data_context", None) is data_context:
        return state
    scoped = copy(state)
    scoped.data_context = data_context
    return scoped


async def catalogs_for_spec(
    state: Any, spec: OntologySpec, sql_agent: Any = None
) -> Dict[str, FieldCatalog]:
    """One field catalog per dataset the ontology references, keyed by dataset id.

    A dataset the session no longer holds is simply absent; ``validate_spec``
    turns that into one error on the entity rather than a failed request.
    """
    catalogs: Dict[str, FieldCatalog] = {}
    for dataset in spec.datasets():
        try:
            dc = resolve_dataset(state, dataset)
        except SourceUnavailableError:
            continue
        try:
            catalogs[dataset] = await get_field_catalog(_scoped_state(state, dc), sql_agent)
        except Exception as exc:  # noqa: BLE001 — one bad dataset must not fail the spec
            logger.warning("[ontology] catalog failed for dataset %s: %s", dataset, exc)
    return catalogs


def dataset_labels(state: Any, spec: OntologySpec) -> Dict[str, str]:
    """Dataset id → display filename, for the UI's entity cards."""
    labels: Dict[str, str] = {}
    for dataset in spec.datasets():
        try:
            labels[dataset] = str(resolve_dataset(state, dataset).filename)
        except SourceUnavailableError:
            continue
    return labels


async def normalize_and_validate(
    state: Any, spec: OntologySpec, sql_agent: Any = None
) -> Tuple[OntologySpec, List[OntologyError], Dict[str, FieldCatalog]]:
    """Canonicalize a spec and report every problem, without raising."""
    catalogs = await catalogs_for_spec(state, spec, sql_agent)
    normalized = normalize_spec(spec, catalogs, dataset_labels(state, spec))
    return normalized, validate_spec(normalized, catalogs), catalogs


# ---------------------------------------------------------------------------
# Joined frames
# ---------------------------------------------------------------------------


def _prefixed(df: pd.DataFrame, entity_name: str) -> pd.DataFrame:
    from .resolve import JOIN_SEP

    return df.rename(columns={c: f"{entity_name}{JOIN_SEP}{c}" for c in df.columns})


def build_joined_frame(
    state: Any, spec: OntologySpec, base_entity: str, joins: List[JoinStep]
) -> pd.DataFrame:
    """Materialize the base entity joined to everything the query reaches.

    Joined-in columns are prefixed with their entity name, so two datasets with
    an ``id`` column cannot collide and ``resolve.joined_column`` can name any
    field unambiguously.
    """
    base = spec.entity(base_entity)
    if base is None:  # pragma: no cover — plan_join guarantees this
        raise OntologyValidationError(
            [OntologyError(path="entities", code="unknown_entity", message="Missing base entity.")]
        )
    frame = resolve_dataset(state, base.dataset).working_dataframe
    if frame is None:
        raise ValueError("No dataset loaded — upload a file first.")
    frame = frame.copy()

    for step in joins:
        right = spec.entity(step.right_entity)
        if right is None:  # pragma: no cover
            continue
        right_df = resolve_dataset(state, right.dataset).working_dataframe
        if right_df is None:
            raise ValueError(f"Entity '{right.name}' has no data loaded.")
        right_df = _prefixed(right_df, step.right_entity)
        left_on = joined_column(step.left_entity, base_entity, step.left_field)
        right_on = joined_column(step.right_entity, base_entity, step.right_field)
        if left_on not in frame.columns:
            raise OntologyValidationError(
                [
                    OntologyError(
                        path="relationships",
                        code="unknown_field",
                        message=f"Join column '{step.left_field}' is missing from "
                        f"'{step.left_entity}'.",
                    )
                ]
            )
        frame = frame.merge(
            right_df,
            how="left" if step.join_type == "left" else "inner",
            left_on=left_on,
            right_on=right_on,
        )
    return frame


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


def _cross_entity_sql_error(entity_names: List[str]) -> OntologyValidationError:
    return OntologyValidationError(
        [
            OntologyError(
                path="metrics",
                code="cross_entity_sql_unsupported",
                message=(
                    "Queries that join "
                    + ", ".join(sorted(entity_names))
                    + " across a warehouse source are not supported yet. "
                    "Query one entity at a time, or ask for these together on file data."
                ),
            )
        ]
    )


async def run_query(
    state: Any,
    spec: OntologySpec,
    metrics: List[str],
    dimensions: List[QueryDimension],
    sql_agent: Any = None,
) -> Tuple[ResolvedQuery, VizData]:
    """Resolve + execute a semantic query. Returns the resolution and its data."""
    base, joins, join_errors = plan_join(spec, metrics, dimensions)
    if base is None or join_errors:
        # An unreachable entity must fail loudly here. Falling through would
        # plan against the base frame alone and quietly answer a different
        # question than the one asked.
        raise OntologyValidationError(join_errors)

    if joins:
        entity_names = [base.name] + [s.right_entity for s in joins]
        datasets = {base.dataset} | {
            e.dataset for e in spec.entities if e.name in set(entity_names)
        }
        for dataset in datasets:
            if resolve_dataset(state, dataset).is_sql_datasource():
                raise _cross_entity_sql_error(entity_names)

        frame = build_joined_frame(state, spec, base.name, joins)
        catalog = build_catalog_pandas(frame)
        resolved = resolve_query(spec, metrics, dimensions, catalog)
        return resolved, await asyncio.to_thread(execute_pandas, frame, resolved.plan)

    dc = resolve_dataset(state, base.dataset)
    scoped = _scoped_state(state, dc)
    catalog = await get_field_catalog(scoped, sql_agent)
    resolved = resolve_query(spec, metrics, dimensions, catalog)

    if dc.is_sql_datasource():
        if sql_agent is None:
            raise ValueError("SQL session requires sql_agent to execute a query.")
        schema, table = dc.get_sql_target()
        if not schema or not table:
            raise ValueError("SQL session has no resolvable table target.")
        effective = _extend_catalog_with_calcs(catalog, resolved)
        sql = compile_spec_sql(resolved.plan, effective, str(schema), str(table))
        # DEBUG, not INFO: compiled SQL inlines metric filter values as literals.
        logger.debug("[ontology] executing query SQL: %s", sql)
        records = await sql_agent.run_sql(scoped, sql)
        return resolved, sql_records_to_vizdata(records, resolved.plan)

    frame = dc.working_dataframe
    if frame is None:
        raise ValueError("No dataset loaded — upload a file first.")
    return resolved, await asyncio.to_thread(execute_pandas, frame, resolved.plan)


def _extend_catalog_with_calcs(catalog: FieldCatalog, resolved: ResolvedQuery) -> FieldCatalog:
    """The SQL compiler resolves measure columns through the catalog, so the
    hidden per-metric projections must be visible to it."""
    from viz.expr import extend_catalog

    return extend_catalog(catalog, resolved.plan.calcs) if resolved.plan.calcs else catalog


async def preview_query(
    state: Any,
    spec: OntologySpec,
    metrics: List[str],
    dimensions: List[QueryDimension],
    sql_agent: Any = None,
) -> Tuple[ResolvedQuery, VizData]:
    """``run_query`` with resolution errors normalized to the validation type."""
    try:
        return await run_query(state, spec, metrics, dimensions, sql_agent=sql_agent)
    except ResolutionError as exc:
        raise OntologyValidationError(exc.errors) from exc


def dimension_options(spec: OntologySpec, metric_name: str) -> List[str]:
    """Dimensions a metric can legally be sliced by: its own entity's, plus any
    reachable through a relationship. Powers the UI's dimension picker."""
    metric = spec.metric(metric_name)
    if metric is None:
        return []
    from .resolve import find_join_path

    out: List[str] = []
    for dim in spec.dimensions:
        if dim.entity == metric.entity or find_join_path(spec, metric.entity, dim.entity):
            out.append(dim.name)
    return out


def missing_datasets(state: Any, spec: OntologySpec) -> List[str]:
    """Datasets the spec references that the session no longer holds."""
    out: List[str] = []
    for dataset in spec.datasets():
        try:
            resolve_dataset(state, dataset)
        except SourceUnavailableError:
            out.append(dataset)
    return out


__all__ = [
    "OntologyValidationError",
    "build_joined_frame",
    "catalogs_for_spec",
    "dataset_labels",
    "dimension_options",
    "missing_datasets",
    "normalize_and_validate",
    "preview_query",
    "resolve_dataset",
    "run_query",
]

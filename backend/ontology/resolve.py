"""
Resolution — a semantic query becomes a ``viz.plan.QueryPlan``.

This is the file that earns the ontology its accuracy claim. The agent names a
metric and some dimensions; everything about *how* that becomes a grouped
aggregate is decided here, deterministically, and executed by the compilers the
viz subsystem already parity-tests. No LLM writes an aggregation.

Two things are worth understanding:

**Metric filters fold into the aggregate, not the WHERE clause.** ``revenue``
(excluding cancelled orders) and ``order_count`` (all orders) can be selected in
one query and each stays correct, because the filter becomes
``if(<filter>, <expr>, null)`` inside that metric's own aggregate. A filtered
count becomes ``sum(if(<filter>, 1, 0))`` — ``count`` counts rows, not non-null
values, so it cannot express "count only these".

**Channel keys are the definition names.** ``QueryPlan`` channels are arbitrary
strings for aggregated plans (both compilers guard nulls over every dimension),
so output columns come back named ``revenue`` and ``region`` rather than
``x``/``y``. Ontology queries are always aggregated, which is what makes that
safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from viz.catalog import FieldCatalog, FieldInfo
from viz.expr import ExprError, ResolvedCalc, infer_type, parse
from viz.plan import AGGREGATED_ROW_CAP, PlannedDim, PlannedMeasure, QueryPlan

from .spec import EntityDef, MetricDef, OntologyError, OntologySpec, RelationshipDef

# Prefix for the hidden per-metric projection a folded filter needs.
_MEASURE_COL = "__m_"
# Columns from a joined entity are prefixed so same-named fields cannot collide.
JOIN_SEP = "__"
# A join path longer than this is almost certainly a modelling mistake, and each
# hop multiplies fan-out risk.
MAX_JOIN_DEPTH = 3


@dataclass
class QueryDimension:
    """One requested grouping, by dimension name."""

    name: str
    time_unit: Optional[str] = None
    top_k: Optional[int] = None


@dataclass
class JoinStep:
    """One hop of a resolved join path, in execution order."""

    relationship: RelationshipDef
    left_entity: str
    right_entity: str
    left_field: str
    right_field: str
    join_type: str


@dataclass
class ResolvedQuery:
    plan: QueryPlan
    base_entity: EntityDef
    joins: List[JoinStep] = field(default_factory=list)
    # Definition names used that are still drafts — surfaced with every answer.
    uncertified: List[str] = field(default_factory=list)

    @property
    def spans_entities(self) -> bool:
        return bool(self.joins)

    @property
    def fan_out_relationships(self) -> List[str]:
        """Relationships on this query's path that duplicate base rows.

        Non-empty means additive measures (sum, count) are inflated. The query
        still runs — sometimes a fanned-out count is exactly what was asked for —
        but no surface may present the number without saying so.
        """
        return sorted({s.relationship.name for s in self.joins if fans_out(s)})


class ResolutionError(Exception):
    """The query cannot be expressed against this ontology; carries errors."""

    def __init__(self, errors: List[OntologyError]):
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors


def _error(path: str, code: str, message: str) -> OntologyError:
    return OntologyError(path=path, code=code, message=message)


# ---------------------------------------------------------------------------
# Join path
# ---------------------------------------------------------------------------


def _adjacency(spec: OntologySpec) -> Dict[str, List[Tuple[str, RelationshipDef]]]:
    """Undirected adjacency — a relationship is traversable from either side."""
    out: Dict[str, List[Tuple[str, RelationshipDef]]] = {}
    for rel in spec.relationships:
        out.setdefault(rel.from_entity, []).append((rel.to_entity, rel))
        out.setdefault(rel.to_entity, []).append((rel.from_entity, rel))
    return out


def find_join_path(
    spec: OntologySpec,
    start: str,
    target: str,
    *,
    adjacency: Optional[Dict[str, List[Tuple[str, RelationshipDef]]]] = None,
) -> Optional[List[JoinStep]]:
    """Shortest relationship path from ``start`` to ``target``, or None.

    Breadth-first, so the fewest hops win. Direction is normalized to the
    traversal direction: ``left`` is the side already in the frame. Callers
    resolving several targets pass one ``adjacency`` (see ``_adjacency``).
    """
    if start == target:
        return []
    if adjacency is None:
        adjacency = _adjacency(spec)
    queue: List[Tuple[str, List[JoinStep]]] = [(start, [])]
    visited = {start}

    while queue:
        node, path = queue.pop(0)
        if len(path) >= MAX_JOIN_DEPTH:
            continue
        for neighbour, rel in adjacency.get(node, []):
            if neighbour in visited:
                continue
            if rel.from_entity == node:
                step = JoinStep(rel, node, neighbour, rel.from_field, rel.to_field, rel.join_type)
            else:
                step = JoinStep(rel, node, neighbour, rel.to_field, rel.from_field, rel.join_type)
            extended = path + [step]
            if neighbour == target:
                return extended
            visited.add(neighbour)
            queue.append((neighbour, extended))
    return None


def fans_out(step: JoinStep) -> bool:
    """True when traversing this step multiplies the rows already in the frame.

    ``cardinality`` is declared from ``from_entity`` → ``to_entity``, so whether a
    hop fans out depends on which way it is being traversed: orders → customers
    (many_to_one) matches at most one customer per order and is safe, while
    customers → orders is the same relationship walked backwards and duplicates
    each customer once per order.
    """
    rel = step.relationship
    if rel.cardinality == "one_to_one":
        return False
    if rel.cardinality == "many_to_many":
        return True
    forward = step.left_entity == rel.from_entity
    # The "many" side is the one that produces multiple matches per left row.
    return rel.cardinality == "one_to_many" if forward else rel.cardinality == "many_to_one"


def joined_column(entity_name: str, base_entity: str, field_name: str) -> str:
    """Column name for ``field_name`` once ``entity_name`` has been joined in."""
    if entity_name == base_entity:
        return field_name
    return f"{entity_name}{JOIN_SEP}{field_name}"


# ---------------------------------------------------------------------------
# Expression assembly
# ---------------------------------------------------------------------------


def _resolve_calc(
    name: str, source: str, catalog: FieldCatalog, path: str
) -> Tuple[Optional[ResolvedCalc], List[OntologyError]]:
    try:
        ast = parse(source)
    except ExprError as exc:
        return None, [_error(path, "bad_expression", str(exc))]

    def resolve(field_name: str) -> Optional[str]:
        info = catalog.get(field_name)
        return info.type if info is not None else None

    try:
        field_type = infer_type(ast, resolve)
    except ExprError as exc:
        code = "unknown_field" if getattr(exc, "unknown_field", None) else "bad_expression"
        return None, [_error(path, code, str(exc))]
    return ResolvedCalc(name=name, expr=source, ast=ast, field_type=field_type), []


def _qualify(expr: str, metric_entity: str, base_entity: str) -> str:
    """No-op today: metric expressions are always evaluated on their own entity.

    Kept as the single seam where cross-entity metric expressions would rewrite
    field references, so callers never re-derive the rule.
    """
    return expr


def metric_projection(metric: MetricDef) -> Tuple[str, Optional[str], str]:
    """``(column, expression, aggregate)`` for one metric.

    The single place a metric's definition becomes something executable. Both
    consumers go through it — the ontology's own planner and the viz bridge —
    so a chart of "revenue" and a chat answer about "revenue" can never compute
    it two different ways.

    ``expression`` is None only for an unfiltered count, which needs no
    projection because COUNT(*) counts rows.
    """
    expr = (metric.expr or "").strip()
    filter_expr = (metric.filter_expr or "").strip()
    column = f"{_MEASURE_COL}{metric.name}"

    if metric.aggregate == "count":
        if not filter_expr:
            return column, None, "count"
        # COUNT(*) counts rows, not non-null values, so a filtered count has to
        # sum a 0/1 indicator instead.
        return column, f"if({filter_expr}, 1, 0)", "sum"

    source = f"if({filter_expr}, ({expr}), null)" if filter_expr else expr
    return column, source, metric.aggregate


def resolve_metric(
    metric: MetricDef,
    catalog: FieldCatalog,
    base_entity: str,
    path: str,
) -> Tuple[Optional[PlannedMeasure], List[ResolvedCalc], List[OntologyError]]:
    """One metric becomes a measure plus, when it needs one, a hidden projection."""
    column, source, aggregate = metric_projection(metric)
    if source is None:
        return PlannedMeasure(channel=metric.name, aggregate="count"), [], []

    where = f"{path}.filterExpr" if metric.aggregate == "count" else f"{path}.expr"
    calc, errors = _resolve_calc(column, source, catalog, where)
    if calc is None:
        return None, [], errors
    return PlannedMeasure(channel=metric.name, aggregate=aggregate, fld=column), [calc], []


# ---------------------------------------------------------------------------
# Query resolution
# ---------------------------------------------------------------------------


def _base_entity(
    spec: OntologySpec, metric_names: List[str], dim_names: List[str]
) -> Optional[str]:
    """The entity a query is anchored on: the first metric's, else the first dimension's."""
    for name in metric_names:
        metric = spec.metric(name)
        if metric is not None:
            return metric.entity
    for name in dim_names:
        dim = spec.dimension(name)
        if dim is not None:
            return dim.entity
    return None


def plan_join(
    spec: OntologySpec, metrics: List[str], dimensions: List[QueryDimension]
) -> Tuple[Optional[EntityDef], List[JoinStep], List[OntologyError]]:
    """Which entities a query touches, and how to reach them from the base.

    Called twice — once by the executor to know what frame to build, once inside
    ``resolve_query`` to bind fields to it — so the two can never disagree about
    the shape of the data being planned against.
    """
    errors: List[OntologyError] = []
    dim_names = [d.name for d in dimensions]
    base_name = _base_entity(spec, metrics, dim_names)
    if base_name is None:
        # Nothing resolved. Say *why* — "no metric named 'profit'" is actionable,
        # "name at least one metric" is not when the caller named four.
        named = [
            _error(f"metrics[{i}]", "unknown_metric", f"No metric named '{name}'.")
            for i, name in enumerate(metrics)
        ] + [
            _error(f"dimensions[{i}]", "unknown_dimension", f"No dimension named '{name}'.")
            for i, name in enumerate(dim_names)
        ]
        return (
            None,
            [],
            named or [_error("metrics", "empty_query", "Name at least one metric or dimension.")],
        )
    base = spec.entity(base_name)
    if base is None:
        return None, [], [_error("metrics", "unknown_entity", f"No entity named '{base_name}'.")]

    joins: List[JoinStep] = []
    joined: set[str] = {base_name}
    adjacency = _adjacency(spec)

    def reach(entity_name: str, path: str) -> None:
        if entity_name in joined:
            return
        steps = find_join_path(spec, base_name, entity_name, adjacency=adjacency)
        if steps is None:
            errors.append(
                _error(
                    path,
                    "no_join_path",
                    f"'{entity_name}' has no relationship path to '{base_name}'. "
                    "Add a relationship between them in the Ontology tab.",
                )
            )
            return
        for step in steps:
            if step.right_entity not in joined:
                joins.append(step)
                joined.add(step.right_entity)

    for i, name in enumerate(metrics):
        metric = spec.metric(name)
        if metric is not None:
            reach(metric.entity, f"metrics[{i}]")
    for i, requested in enumerate(dimensions):
        dim = spec.dimension(requested.name)
        if dim is not None:
            reach(dim.entity, f"dimensions[{i}]")
    return base, joins, errors


def resolve_query(
    spec: OntologySpec,
    metrics: List[str],
    dimensions: List[QueryDimension],
    catalog: FieldCatalog,
    *,
    row_cap: int = AGGREGATED_ROW_CAP,
) -> ResolvedQuery:
    """Turn named metrics + dimensions into an executable plan.

    ``catalog`` describes the frame the plan will run against — the base entity's
    dataset for a single-entity query, or the joined frame's effective catalog
    when the query spans entities (see ``executor.build_joined_frame``).

    Raises ``ResolutionError`` with structured errors; it never returns a plan
    that the compilers would then fail on.
    """
    base, joins, errors = plan_join(spec, metrics, dimensions)
    if base is None:
        raise ResolutionError(errors)
    base_name = base.name
    reachable = {base_name} | {step.right_entity for step in joins}
    uncertified: List[str] = []

    measures: List[PlannedMeasure] = []
    calcs: List[ResolvedCalc] = []
    for i, name in enumerate(metrics):
        path = f"metrics[{i}]"
        metric = spec.metric(name)
        if metric is None:
            errors.append(_error(path, "unknown_metric", f"No metric named '{name}'."))
            continue
        if metric.entity not in reachable:
            continue  # plan_join already recorded why
        if not metric.certified:
            uncertified.append(metric.name)
        measure, metric_calcs, metric_errors = resolve_metric(metric, catalog, base_name, path)
        errors.extend(metric_errors)
        if measure is not None:
            measures.append(measure)
            calcs.extend(metric_calcs)

    dims: List[PlannedDim] = []
    for i, requested in enumerate(dimensions):
        path = f"dimensions[{i}]"
        dim = spec.dimension(requested.name)
        if dim is None:
            errors.append(
                _error(path, "unknown_dimension", f"No dimension named '{requested.name}'.")
            )
            continue
        if dim.entity not in reachable:
            continue  # plan_join already recorded why
        if not dim.certified:
            uncertified.append(dim.name)

        if not dim.field:
            errors.append(
                _error(path, "missing_field", f"Dimension '{dim.name}' has no column set.")
            )
            continue
        column = joined_column(dim.entity, base_name, dim.field)
        info: Optional[FieldInfo] = catalog.get(column)
        if info is None:
            errors.append(
                _error(path, "unknown_field", f"Column '{dim.field}' is not in the query's data.")
            )
            continue
        if requested.time_unit and info.type != "temporal":
            errors.append(_error(path, "not_temporal", f"'{dim.name}' is not a date column."))
            continue
        dims.append(
            PlannedDim(
                channel=dim.name,
                fld=column,
                kind="time" if requested.time_unit else "raw",
                field_type=info.type,
                time_unit=requested.time_unit,
                top_k=(requested.top_k, False) if requested.top_k else None,
            )
        )

    if not measures and not errors:
        errors.append(_error("metrics", "no_measures", "A query needs at least one metric."))
    if errors:
        raise ResolutionError(errors)

    plan = QueryPlan(
        aggregated=True,
        dims=dims,
        measures=measures,
        calcs=calcs,
        # Most-significant sort: the first measure, descending, so "top regions
        # by revenue" needs no explicit ordering from the caller.
        order=[(measures[0].channel, "desc")] if measures else [],
        row_cap=row_cap,
    )
    return ResolvedQuery(
        plan=plan,
        base_entity=base,
        joins=joins,
        uncertified=sorted(set(uncertified)),
    )

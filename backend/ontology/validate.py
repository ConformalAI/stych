"""
Validation — an OntologySpec checked against the real field catalogs.

Pure and total: ``validate_spec`` never raises, it returns a list of structured
``OntologyError``s (path / code / message), the same contract ``viz.validate``
gives the visualization editor. The UI renders them next to the offending field
and the agent gets them back as a tool observation to self-correct from.

Catalogs are keyed by dataset id (``DataContext.file_id``), because that is what
an entity points at. A dataset the session no longer has produces one error on
the entity rather than a cascade on everything defined over it.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from viz.catalog import FieldCatalog
from viz.expr import ExprError, infer_type, parse

from .spec import MetricDef, OntologyError, OntologySpec

# Aggregates whose expression must be numeric.
_NUMERIC_AGGREGATES = frozenset({"sum", "mean"})

Catalogs = Dict[str, FieldCatalog]


def _field_names(catalog: Optional[FieldCatalog]) -> List[str]:
    return catalog.field_names() if catalog is not None else []


def _has_field(catalog: Optional[FieldCatalog], name: str) -> bool:
    return catalog is not None and catalog.get(name) is not None


def _missing_field_error(path: str, what: str) -> OntologyError:
    return OntologyError(
        path=path,
        code="missing_field",
        message=f"Pick a column for this {what}.",
    )


def _unknown_field_error(path: str, field: str, catalog: Optional[FieldCatalog]) -> OntologyError:
    known = _field_names(catalog)
    hint = ""
    if known:
        preview = ", ".join(known[:8])
        hint = f" Available: {preview}{'…' if len(known) > 8 else ''}"
    return OntologyError(
        path=path,
        code="unknown_field",
        message=f"Column '{field}' is not in this entity's dataset.{hint}",
    )


def normalize_spec(
    spec: OntologySpec, catalogs: Catalogs, labels: Optional[Dict[str, str]] = None
) -> OntologySpec:
    """Fill in what can be derived, so the UI and agent see one canonical form.

    Only ``dataset_label`` is derived, from the caller's dataset-id → filename
    map (``FieldCatalog.source`` is the file id, not a display name). Primary
    keys and title fields are deliberately NOT guessed — an ontology that
    silently invents an identity is worse than one that leaves the field blank
    for a human to set.
    """
    if not labels:
        return spec
    entities = []
    for entity in spec.entities:
        label = labels.get(entity.dataset)
        if entity.dataset_label or not label:
            entities.append(entity)
            continue
        entities.append(entity.model_copy(update={"dataset_label": label}))
    return spec.model_copy(update={"entities": entities})


def _validate_entities(spec: OntologySpec, catalogs: Catalogs) -> List[OntologyError]:
    errors: List[OntologyError] = []
    for i, entity in enumerate(spec.entities):
        path = f"entities[{i}]"
        catalog = catalogs.get(entity.dataset)
        if catalog is None:
            errors.append(
                OntologyError(
                    path=f"{path}.dataset",
                    code="unknown_dataset",
                    message=(
                        f"Entity '{entity.name}' points at a dataset that is not loaded "
                        "in this session."
                    ),
                )
            )
            continue
        if entity.primary_key and not _has_field(catalog, entity.primary_key):
            errors.append(_unknown_field_error(f"{path}.primaryKey", entity.primary_key, catalog))
        if entity.title_field and not _has_field(catalog, entity.title_field):
            errors.append(_unknown_field_error(f"{path}.titleField", entity.title_field, catalog))
    return errors


def _validate_relationships(spec: OntologySpec, catalogs: Catalogs) -> List[OntologyError]:
    errors: List[OntologyError] = []
    seen_pairs: set[tuple[str, str, str, str]] = set()

    for i, rel in enumerate(spec.relationships):
        path = f"relationships[{i}]"
        for side, entity_name, field_name in (
            ("from", rel.from_entity, rel.from_field),
            ("to", rel.to_entity, rel.to_field),
        ):
            entity = spec.entity(entity_name)
            if entity is None:
                errors.append(
                    OntologyError(
                        path=f"{path}.{side}Entity",
                        code="unknown_entity",
                        message=f"No entity named '{entity_name}'.",
                    )
                )
                continue
            if not field_name:
                errors.append(_missing_field_error(f"{path}.{side}Field", "side of the join"))
                continue
            catalog = catalogs.get(entity.dataset)
            if catalog is not None and not _has_field(catalog, field_name):
                errors.append(_unknown_field_error(f"{path}.{side}Field", field_name, catalog))

        key = (rel.from_entity, rel.from_field, rel.to_entity, rel.to_field)
        if key in seen_pairs:
            errors.append(
                OntologyError(
                    path=path,
                    code="duplicate_relationship",
                    message=(
                        f"'{rel.from_entity}.{rel.from_field}' is already joined to "
                        f"'{rel.to_entity}.{rel.to_field}'."
                    ),
                )
            )
        seen_pairs.add(key)
    return errors


def _entity_catalog(
    spec: OntologySpec, catalogs: Catalogs, entity_name: str
) -> Optional[FieldCatalog]:
    entity = spec.entity(entity_name)
    return catalogs.get(entity.dataset) if entity is not None else None


def _validate_dimensions(spec: OntologySpec, catalogs: Catalogs) -> List[OntologyError]:
    errors: List[OntologyError] = []
    for i, dim in enumerate(spec.dimensions):
        path = f"dimensions[{i}]"
        if spec.entity(dim.entity) is None:
            errors.append(
                OntologyError(
                    path=f"{path}.entity",
                    code="unknown_entity",
                    message=f"No entity named '{dim.entity}'.",
                )
            )
            continue
        if not dim.field:
            errors.append(_missing_field_error(f"{path}.field", "dimension"))
            continue
        catalog = _entity_catalog(spec, catalogs, dim.entity)
        if catalog is not None and not _has_field(catalog, dim.field):
            errors.append(_unknown_field_error(f"{path}.field", dim.field, catalog))
    return errors


def _validate_expression(
    expr: str,
    path: str,
    catalog: Optional[FieldCatalog],
    *,
    expect_boolean: bool = False,
    expect_numeric: bool = False,
) -> List[OntologyError]:
    """Parse + type one viz.expr expression against an entity's catalog."""
    if catalog is None:
        return []
    try:
        ast = parse(expr)
    except ExprError as exc:
        return [OntologyError(path=path, code="bad_expression", message=str(exc))]

    def resolve(name: str) -> Optional[str]:
        info = catalog.get(name)
        return info.type if info is not None else None

    try:
        result_type = infer_type(ast, resolve)
    except ExprError as exc:
        code = "unknown_field" if getattr(exc, "unknown_field", None) else "bad_expression"
        return [OntologyError(path=path, code=code, message=str(exc))]

    if expect_boolean and result_type != "boolean":
        return [
            OntologyError(
                path=path,
                code="not_boolean",
                message="A metric filter must be a condition, e.g. status != 'cancelled'.",
            )
        ]
    if expect_numeric and result_type != "quantitative":
        return [
            OntologyError(
                path=path,
                code="not_numeric",
                message="This aggregate needs a numeric expression.",
            )
        ]
    return []


def _validate_metric(
    metric: MetricDef, path: str, spec: OntologySpec, catalogs: Catalogs
) -> List[OntologyError]:
    if spec.entity(metric.entity) is None:
        return [
            OntologyError(
                path=f"{path}.entity",
                code="unknown_entity",
                message=f"No entity named '{metric.entity}'.",
            )
        ]

    errors: List[OntologyError] = []
    catalog = _entity_catalog(spec, catalogs, metric.entity)

    if metric.expr:
        errors += _validate_expression(
            metric.expr,
            f"{path}.expr",
            catalog,
            expect_numeric=metric.aggregate in _NUMERIC_AGGREGATES,
        )
    if metric.filter_expr:
        errors += _validate_expression(
            metric.filter_expr, f"{path}.filterExpr", catalog, expect_boolean=True
        )

    time_field = metric.default_time_field
    if time_field and catalog is not None:
        info = catalog.get(time_field)
        if info is None:
            errors.append(_unknown_field_error(f"{path}.defaultTimeField", time_field, catalog))
        elif info.type != "temporal":
            errors.append(
                OntologyError(
                    path=f"{path}.defaultTimeField",
                    code="not_temporal",
                    message=f"'{time_field}' is not a date column.",
                )
            )
    return errors


def validate_spec(spec: OntologySpec, catalogs: Catalogs) -> List[OntologyError]:
    """Every structural problem in the spec, in spec order. Never raises."""
    errors = _validate_entities(spec, catalogs)
    errors += _validate_relationships(spec, catalogs)
    errors += _validate_dimensions(spec, catalogs)
    for i, metric in enumerate(spec.metrics):
        errors += _validate_metric(metric, f"metrics[{i}]", spec, catalogs)
    return errors

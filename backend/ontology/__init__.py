"""
Ontology — the semantic layer: what the business calls things, and how they relate.

An ``OntologySpec`` names the entities a session's datasets represent, the
relationships between them, and the dimensions and metrics defined on top. It is
the layer that turns "revenue by region" from something an LLM re-derives per
question into something the backend resolves deterministically.

Public surface:
    spec      — the Pydantic model tree (source of truth)
    validate  — normalize + validate against field catalogs, never raises
    resolve   — a semantic query becomes a viz QueryPlan (no third engine)
    executor  — the single entry point: resolve → execute
"""

from .spec import (
    SCHEMA_VERSION,
    DimensionDef,
    EntityDef,
    MetricDef,
    OntologyError,
    OntologySpec,
    Provenance,
    RelationshipDef,
)

__all__ = [
    "SCHEMA_VERSION",
    "DimensionDef",
    "EntityDef",
    "MetricDef",
    "OntologyError",
    "OntologySpec",
    "Provenance",
    "RelationshipDef",
]

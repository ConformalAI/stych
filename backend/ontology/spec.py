"""
OntologySpec — the declarative semantic model (schema v1).

One spec per session, spanning every dataset in it. Four kinds of definition:

- **entities** — what a dataset *is*. ``orders`` is an entity backed by one
  ``DataContext``, with a primary key and the words users say for it.
- **relationships** — how entities join. ``orders.customer_id → customers.id``,
  with cardinality. This is what lets "revenue by customer region" span two
  files without an agent inventing a join.
- **dimensions** — a field you may slice by, plus the vocabulary to find it.
  ``value_synonyms`` maps stored values to spoken ones (``"CA"`` → California).
- **metrics** — an aggregate over a ``viz.expr`` expression, optionally with a
  filter that belongs to the metric rather than the query.

Every definition carries ``certified``. Uncertified definitions still resolve —
they are drafts, usually agent-proposed — but every surface that consumes one
must say so, because a confidently wrong metric is worse than no metric.

Expressions reuse ``viz.expr`` verbatim and metrics resolve into
``viz.plan.QueryPlan``, so the ontology adds no third execution engine.

Names live in ONE namespace across all four kinds: when an agent asks for
"region" it must never be ambiguous which definition it meant.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from viz.spec import Aggregate, TimeUnit

SCHEMA_VERSION = 1

# Aggregates that need something to aggregate. ``count`` is the only one that
# can stand alone (COUNT(*) over the group).
_NEEDS_EXPR = frozenset({"sum", "mean", "min", "max", "distinct"})

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_NAME_LEN = 60
MAX_ENTITIES = 50
MAX_RELATIONSHIPS = 100
MAX_DIMENSIONS = 300
MAX_METRICS = 300
MAX_SYNONYMS = 20

Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
JoinType = Literal["inner", "left"]


class _OntologyModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be at most {MAX_NAME_LEN} characters")
    if not _NAME_RE.match(name):
        raise ValueError(
            "name must be lower_snake_case: start with a letter, then letters, digits or underscores"
        )
    return name


def _clean_synonyms(values: Optional[List[str]]) -> List[str]:
    """Deduplicate case-insensitively, preserving first-seen order."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[:MAX_SYNONYMS]


def _clean_required(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


class Provenance(_OntologyModel):
    """Who authored a definition and why — carried into every version row."""

    author: Literal["user", "agent"] = "user"
    # The natural-language request or reasoning that produced this definition.
    intent: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[str] = Field(default=None, alias="createdAt")


class _Definition(_OntologyModel):
    """Fields every definition shares."""

    name: str
    label: Optional[str] = None
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    certified: bool = False
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        return _validate_name(name)

    @field_validator("synonyms")
    @classmethod
    def _check_synonyms(cls, values: List[str]) -> List[str]:
        return _clean_synonyms(values)

    @property
    def display_label(self) -> str:
        return self.label or self.name


class EntityDef(_Definition):
    """A dataset, named as the business thing it represents.

    ``dataset`` is a stable ``DataContext.file_id`` — the same identity
    ``VizSpec.data.source`` uses, so an entity and a chart always agree on which
    data they mean. ``dataset_label`` is the filename, carried for display only
    so the UI can render a readable name without resolving the session.
    """

    dataset: str
    dataset_label: Optional[str] = Field(default=None, alias="datasetLabel")
    primary_key: Optional[str] = Field(default=None, alias="primaryKey")
    # The column that best names one row — shown when an object is referenced.
    title_field: Optional[str] = Field(default=None, alias="titleField")
    # Canvas position, persisted so the graph layout survives a reload.
    x: Optional[float] = None
    y: Optional[float] = None

    @field_validator("dataset")
    @classmethod
    def _check_dataset(cls, dataset: str) -> str:
        return _clean_required(dataset, "dataset")


class RelationshipDef(_OntologyModel):
    """A join between two entities, declared once.

    Direction matters for cardinality: ``many_to_one`` means many ``from`` rows
    per one ``to`` row (orders → customers). ``resolve.fans_out`` reads it to
    decide whether traversing a hop duplicates the rows already in the frame,
    and every surface that reports a number warns when it does.
    """

    name: str
    from_entity: str = Field(alias="fromEntity")
    from_field: str = Field(alias="fromField")
    to_entity: str = Field(alias="toEntity")
    to_field: str = Field(alias="toField")
    cardinality: Cardinality = "many_to_one"
    join_type: JoinType = Field(default="left", alias="joinType")
    label: Optional[str] = None
    description: Optional[str] = None
    certified: bool = False
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        return _validate_name(name)

    @field_validator("from_entity", "to_entity")
    @classmethod
    def _check_entity(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("from_field", "to_field")
    @classmethod
    def _check_field(cls, value: str) -> str:
        # May be blank while the user is still picking a column — see the note
        # on DimensionDef.field. ``validate_spec`` reports it as missing_field.
        return (value or "").strip()

    @model_validator(mode="after")
    def _check_not_self(self) -> "RelationshipDef":
        if self.from_entity == self.to_entity:
            raise ValueError("a relationship must connect two different entities")
        return self

    @property
    def display_label(self) -> str:
        return self.label or self.name


class DimensionDef(_Definition):
    """A field a metric may be sliced by, plus the vocabulary to find it.

    ``value_synonyms`` maps a stored value to the words users say for it
    (``{"CA": ["California"]}``) so an agent can translate a question into a
    filter without guessing at the encoding.
    """

    entity: str
    field: str
    value_synonyms: Dict[str, List[str]] = Field(default_factory=dict, alias="valueSynonyms")

    @field_validator("entity")
    @classmethod
    def _check_entity(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("field")
    @classmethod
    def _check_field(cls, value: str) -> str:
        # Deliberately permits "": the editor creates a dimension before the
        # user picks its column, and a half-built ontology must still save or
        # the editor cannot hold work in progress. ``validate_spec`` surfaces it
        # as a structured missing_field error the inspector renders inline.
        return (value or "").strip()

    @field_validator("value_synonyms")
    @classmethod
    def _check_value_synonyms(cls, values: Dict[str, List[str]]) -> Dict[str, List[str]]:
        return {str(k): _clean_synonyms(v) for k, v in (values or {}).items()}


class MetricDef(_Definition):
    """A named aggregate.

    ``expr`` is a row-level ``viz.expr`` expression (the thing being
    aggregated); ``filter_expr`` is a boolean ``viz.expr`` condition scoped to
    THIS metric. The resolver folds the filter into the aggregate rather than the
    query's WHERE clause, so two metrics with different filters can be selected
    in one query and each stays correct.
    """

    entity: str
    expr: Optional[str] = None
    aggregate: Aggregate = "sum"
    filter_expr: Optional[str] = Field(default=None, alias="filterExpr")
    # Default time field/grain for "revenue over time" with no stated grain.
    default_time_field: Optional[str] = Field(default=None, alias="defaultTimeField")
    default_time_unit: Optional[TimeUnit] = Field(default=None, alias="defaultTimeUnit")
    # Free-form display hint (e.g. "currency", "percent"). Presentation only —
    # never affects how the metric is computed.
    format: Optional[str] = None

    @field_validator("entity")
    @classmethod
    def _check_entity(cls, value: str) -> str:
        return _validate_name(value)

    @model_validator(mode="after")
    def _check_expr_required(self) -> "MetricDef":
        expr = (self.expr or "").strip()
        if self.aggregate in _NEEDS_EXPR and not expr:
            raise ValueError(f"aggregate '{self.aggregate}' requires an expression")
        self.expr = expr or None
        self.filter_expr = (self.filter_expr or "").strip() or None
        return self


class OntologySpec(_OntologyModel):
    """Every definition for one session, across all its datasets."""

    schema_version: Literal[1] = Field(default=SCHEMA_VERSION, alias="schemaVersion")
    title: Optional[str] = None
    entities: List[EntityDef] = Field(default_factory=list, max_length=MAX_ENTITIES)
    relationships: List[RelationshipDef] = Field(default_factory=list, max_length=MAX_RELATIONSHIPS)
    dimensions: List[DimensionDef] = Field(default_factory=list, max_length=MAX_DIMENSIONS)
    metrics: List[MetricDef] = Field(default_factory=list, max_length=MAX_METRICS)

    # name -> definition per kind, built alongside the uniqueness check so a
    # lookup never scans the lists. Rebuilt on ``model_copy`` because Pydantic
    # copies private state verbatim and an ``update`` would leave it stale.
    _index: Dict[str, Dict[str, Any]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_unique_names(self) -> "OntologySpec":
        # One namespace across all four kinds: "region" must be unambiguous.
        seen: Dict[str, str] = {}
        index: Dict[str, Dict[str, Any]] = {}
        for kind, items in (
            ("entity", self.entities),
            ("relationship", self.relationships),
            ("dimension", self.dimensions),
            ("metric", self.metrics),
        ):
            by_name = index.setdefault(kind, {})
            for item in items:
                prior = seen.get(item.name)
                if prior == kind:
                    raise ValueError(f"duplicate {kind} name '{item.name}'")
                if prior is not None:
                    raise ValueError(f"name '{item.name}' is used as both a {prior} and a {kind}")
                seen[item.name] = kind
                by_name[item.name] = item
        self._index = index
        return self

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None, deep: bool = False):
        copied = super().model_copy(update=update, deep=deep)
        if update:
            copied._check_unique_names()
        return copied

    # -- lookups ---------------------------------------------------------

    def entity(self, name: str) -> Optional[EntityDef]:
        return self._index.get("entity", {}).get(name)

    def relationship(self, name: str) -> Optional[RelationshipDef]:
        return self._index.get("relationship", {}).get(name)

    def dimension(self, name: str) -> Optional[DimensionDef]:
        return self._index.get("dimension", {}).get(name)

    def metric(self, name: str) -> Optional[MetricDef]:
        return self._index.get("metric", {}).get(name)

    def entity_names(self) -> List[str]:
        return [e.name for e in self.entities]

    def metric_names(self) -> List[str]:
        return [m.name for m in self.metrics]

    def dimension_names(self) -> List[str]:
        return [d.name for d in self.dimensions]

    def datasets(self) -> List[str]:
        """Distinct dataset ids the ontology covers, in entity order."""
        out: List[str] = []
        for e in self.entities:
            if e.dataset not in out:
                out.append(e.dataset)
        return out

    def is_certified(self) -> bool:
        """True when every definition in the spec has been signed off."""
        items = [*self.entities, *self.relationships, *self.dimensions, *self.metrics]
        return bool(items) and all(i.certified for i in items)

    def to_json_dict(self) -> dict:
        """Canonical wire form (alias keys), matching VizSpec.to_json_dict."""
        return self.model_dump(by_alias=True, mode="json")


class OntologyError(BaseModel):
    """One structured problem; ``path`` is a dotted spec location."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.path}: {self.message}"

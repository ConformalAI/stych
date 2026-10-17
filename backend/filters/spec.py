"""
FilterSpec: the declarative contract for custom preview filters.

Three stages, three flexibility budgets:

- **annotate** — derived columns keyed by row identity. Builtin analyzers
  (fuzzy/exact duplicates) or persisted agent-written pandas/SQL code. This is
  where open-ended power lives; anything computable becomes a column.
- **select** — a predicate tree over base ∪ annotation columns. Compiles to a
  pandas mask (``predicate_pandas``) and a SQL WHERE (``predicate_sql``).
  Deliberately no arithmetic: derived scalars are one-line annotate steps.
- **present** — rendering rules (row/cell highlights, severity, tooltip
  templates) plus group banding for paired rows.

Specs are persisted as JSON and re-materialized on demand; data is never
stored with a filter.

Pandas-step code contract: the executor writes the input frame (including a
``__row_key__`` column) to ``filter_input.parquet`` in the session workspace;
the step's code reads it and must write ``filter_annotations/<step_id>.parquet``
containing ``__row_key__`` plus exactly the declared output columns.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated

SPEC_VERSION = 1
ROW_KEY_COLUMN = "__row_key__"
MAX_SPEC_BYTES = 64 * 1024
MAX_REGEX_LENGTH = 500

# Highest wins when several row rules match the same row.
SEVERITY_ORDER = ["danger", "warning", "accent", "info", "success"]
Severity = Literal["danger", "warning", "accent", "info", "success"]

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


# ------------------------------------------------------------------ identity


class RowIdentity(BaseModel):
    """How rows are identified for annotation joins.

    The *resolved* identity ("key:<col>" | "positional" | "none") is
    data-dependent: it is reported in every apply response, never persisted.
    """

    mode: Literal["auto", "key", "positional"] = "auto"
    key: Optional[str] = None

    @model_validator(mode="after")
    def _key_required_for_key_mode(self) -> "RowIdentity":
        if self.mode == "key" and not self.key:
            raise ValueError("identity.key is required when mode is 'key'")
        return self


# ------------------------------------------------------------------ annotate


class AnnotationOutput(BaseModel):
    name: str
    dtype: Literal["number", "string", "bool"] = "string"
    role: Optional[Literal["group", "score", "flag", "ref"]] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _OUTPUT_NAME_RE.match(v):
            raise ValueError(f"output name {v!r} must be a simple identifier")
        if v == ROW_KEY_COLUMN:
            raise ValueError(f"{ROW_KEY_COLUMN} is reserved")
        return v


class _StepBase(BaseModel):
    id: str
    outputs: List[AnnotationOutput] = []

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"step id {v!r} must match {_ID_RE.pattern}")
        return v


class FuzzyDuplicatesStep(_StepBase):
    """Group rows whose value in ``on`` fuzzy-matches another row's value.

    Exact duplicates always group (score 1.0); fuzzy pairs above ``threshold``
    are merged transitively (union-find).
    """

    kind: Literal["fuzzy_duplicates"]
    on: str
    strategy: str = "token_sort"
    threshold: float = Field(0.9, ge=0.0, le=1.0)
    max_rows: int = Field(50_000, ge=100, le=200_000)

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        from tools.linking_fuzzy_tools import available_strategies  # noqa: PLC0415

        if v not in available_strategies():
            raise ValueError(f"unknown strategy {v!r}; use one of {available_strategies()}")
        return v

    @model_validator(mode="after")
    def _default_outputs(self) -> "FuzzyDuplicatesStep":
        if not self.outputs:
            self.outputs = [
                AnnotationOutput(name=f"{self.id}_group", dtype="number", role="group"),
                AnnotationOutput(name=f"{self.id}_score", dtype="number", role="score"),
                AnnotationOutput(name=f"{self.id}_match_key", dtype="string", role="ref"),
            ]
        return self


class DuplicateValuesStep(_StepBase):
    """Group rows sharing identical values across ``columns`` (exact dupes)."""

    kind: Literal["duplicate_values"]
    columns: List[str] = Field(..., min_length=1)
    min_count: int = Field(2, ge=2)

    @model_validator(mode="after")
    def _default_outputs(self) -> "DuplicateValuesStep":
        if not self.outputs:
            self.outputs = [
                AnnotationOutput(name=f"{self.id}_count", dtype="number", role="flag"),
                AnnotationOutput(name=f"{self.id}_group", dtype="number", role="group"),
            ]
        return self


class PandasStep(_StepBase):
    """Persisted agent-written pandas code (see module docstring contract)."""

    kind: Literal["pandas"]
    code: str = Field(..., min_length=1, max_length=20_000)
    outputs: List[AnnotationOutput] = Field(..., min_length=1)


class SqlStep(_StepBase):
    """Persisted agent-written SELECT, materialized into ``stych_temp``.

    Must select ``key_column`` (the source row key) plus the declared outputs.
    ``{table}`` interpolates to the active working table. SQL-source only.
    """

    kind: Literal["sql"]
    sql: str = Field(..., min_length=1, max_length=20_000)
    key_column: str
    outputs: List[AnnotationOutput] = Field(..., min_length=1)


AnnotateStep = Annotated[
    Union[FuzzyDuplicatesStep, DuplicateValuesStep, PandasStep, SqlStep],
    Field(discriminator="kind"),
]


# -------------------------------------------------------------------- select


class ColumnOperand(BaseModel):
    """Marks the right-hand side of a comparison as another column."""

    column: str


Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr, None]
Operand = Union[ColumnOperand, Scalar]


class ComparePred(BaseModel):
    op: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    column: str
    value: Operand

    @model_validator(mode="after")
    def _no_null_literal(self) -> "ComparePred":
        if self.value is None:
            raise ValueError(f"{self.op}: use is_null/not_null instead of a null literal")
        return self


class BetweenPred(BaseModel):
    op: Literal["between"]
    column: str
    low: Operand
    high: Operand


class StringPred(BaseModel):
    op: Literal["contains", "startswith", "endswith", "regex"]
    column: str
    pattern: str = Field(..., min_length=1, max_length=MAX_REGEX_LENGTH)
    case_sensitive: bool = False

    @model_validator(mode="after")
    def _regex_compiles(self) -> "StringPred":
        if self.op == "regex":
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        return self


class MembershipPred(BaseModel):
    op: Literal["in", "not_in"]
    column: str
    values: List[Scalar] = Field(..., min_length=1)


class NullPred(BaseModel):
    op: Literal["is_null", "not_null"]
    column: str


class BoolPred(BaseModel):
    op: Literal["and", "or"]
    children: List["Predicate"] = Field(..., min_length=1)


class NotPred(BaseModel):
    op: Literal["not"]
    child: "Predicate"


Predicate = Annotated[
    Union[ComparePred, BetweenPred, StringPred, MembershipPred, NullPred, BoolPred, NotPred],
    Field(discriminator="op"),
]

BoolPred.model_rebuild()
NotPred.model_rebuild()


def predicate_columns(pred: Any) -> List[str]:
    """All column names referenced by a predicate tree (depth-first, dupes kept)."""
    if isinstance(pred, (BoolPred,)):
        return [c for child in pred.children for c in predicate_columns(child)]
    if isinstance(pred, NotPred):
        return predicate_columns(pred.child)
    cols = [pred.column]
    for operand in (
        getattr(pred, "value", None),
        getattr(pred, "low", None),
        getattr(pred, "high", None),
    ):
        if isinstance(operand, ColumnOperand):
            cols.append(operand.column)
    return cols


# ------------------------------------------------------------------- present


class PresentRule(BaseModel):
    id: str
    target: Literal["row", "cells"] = "row"
    columns: Optional[List[str]] = None
    predicate: Optional[Predicate] = None  # None => the filter's select predicate
    severity: Severity = "warning"
    label: Optional[str] = Field(None, max_length=60)
    tooltip: Optional[str] = Field(None, max_length=200)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"rule id {v!r} must match {_ID_RE.pattern}")
        return v

    @model_validator(mode="after")
    def _cells_need_columns(self) -> "PresentRule":
        if self.target != "cells":
            return self
        if not self.columns:
            raise ValueError("cells rules must list target columns")
        if len(self.columns) > 1 and self.predicate is None:
            raise ValueError(
                "cells rules with more than one column must set an explicit predicate; "
                "omitting it falls back to the row-level select predicate, which would mark "
                "every listed column whenever the row matches, even for columns that aren't "
                "individually the reason. Use one rule per column, each with its own "
                "column-scoped predicate, for independent per-column conditions."
            )
        return self


class Presentation(BaseModel):
    rules: List[PresentRule] = []
    group_by: Optional[str] = None
    matches_only: bool = False


# ----------------------------------------------------------------- FilterSpec


class FilterSpec(BaseModel):
    version: int = SPEC_VERSION
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    identity: RowIdentity = RowIdentity()
    annotate: List[AnnotateStep] = []
    select: Optional[Predicate] = None  # None => every row matches
    present: Presentation = Presentation()

    @model_validator(mode="after")
    def _cross_field(self) -> "FilterSpec":
        step_ids = [s.id for s in self.annotate]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("annotate step ids must be unique")

        outputs: List[str] = []
        for step in self.annotate:
            outputs.extend(o.name for o in step.outputs)
        if len(outputs) != len(set(outputs)):
            raise ValueError("annotation output names must be unique across steps")

        rule_ids = [r.id for r in self.present.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("present rule ids must be unique")

        if self.present.group_by and self.present.group_by not in outputs:
            raise ValueError(
                f"group_by {self.present.group_by!r} is not a declared annotation output"
            )

        if len(self.model_dump_json()) > MAX_SPEC_BYTES:
            raise ValueError(f"spec exceeds {MAX_SPEC_BYTES} bytes")
        return self

    def annotation_outputs(self) -> Dict[str, AnnotationOutput]:
        """Declared annotation columns by name (insertion-ordered)."""
        return {o.name: o for step in self.annotate for o in step.outputs}

    def annotate_fingerprint(self) -> str:
        """Content hash of the stages that determine annotation results."""
        payload = {
            "identity": self.identity.model_dump(),
            "annotate": [s.model_dump() for s in self.annotate],
        }
        canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha1(canon.encode()).hexdigest()


# ------------------------------------------------------------------ helpers


def validation_errors(exc: ValidationError) -> List[Dict[str, str]]:
    """Flatten a pydantic error into the API's ``{path, code, message}`` list."""
    return [
        {
            "path": ".".join(str(part) for part in err["loc"]),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]


def parse_spec(payload: Any) -> FilterSpec:
    """Validate a raw payload into a FilterSpec; raises pydantic ValidationError."""
    if isinstance(payload, FilterSpec):
        return payload
    return FilterSpec.model_validate(payload)

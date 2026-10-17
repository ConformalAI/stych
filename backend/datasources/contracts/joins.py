"""Typed intent and runtime resolution contracts for data joins.

This module deliberately contains no connector routing or SQL generation.  A
``JoinSpec`` is the connector-neutral, agent-authored request; a
``ResolvedJoinPlan`` records the deterministic execution decision that a
resolver made for the attached sources.  Keeping those two shapes separate
prevents a source-side capability from being confused with a materialized
snapshot fallback.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .capabilities import DataFreshness, DataScope, Executor

JOIN_SCHEMA_VERSION = 1


class JoinType(str, Enum):
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL_OUTER = "FULL OUTER"


class JoinStrategy(str, Enum):
    """Strategies currently implemented by the linking subsystem."""

    EXACT = "exact"
    LEVENSHTEIN = "levenshtein"
    JARO_WINKLER = "jaro_winkler"
    TOKEN_SORT = "token_sort"
    TOKEN_SET = "token_set"
    PARTIAL_RATIO = "partial_ratio"
    COSINE_TFIDF = "cosine_tfidf"
    SOUNDEX = "soundex"
    METAPHONE = "metaphone"
    SEMANTIC = "semantic"


class JoinEgress(str, Enum):
    """How much input data leaves its source engine during execution."""

    NONE = "none"
    PREVIEW_ONLY = "preview_only"
    FULL = "full"


class JoinResultMode(str, Enum):
    SOURCE_SCRATCH = "source_scratch"
    DUCKDB_SNAPSHOT = "duckdb_snapshot"
    PANDAS_MATERIALIZED = "pandas_materialized"
    HYBRID_MATERIALIZED = "hybrid_materialized"


class _JoinModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JoinInput(_JoinModel):
    """A source reference safe to put in an agent-authored join plan."""

    source_id: str = Field(min_length=1, max_length=512)
    label: Optional[str] = Field(default=None, max_length=512)


class JoinKey(_JoinModel):
    """One equality key pair. Multiple pairs are combined with ``AND``."""

    left: str = Field(min_length=1, max_length=256)
    right: str = Field(min_length=1, max_length=256)


class JoinStep(_JoinModel):
    """One step in a chained join plan.

    ``left_on``/``right_on`` are retained as a compatibility form for the
    current agent payload.  New callers should use ``keys`` so multi-key joins
    have one canonical representation.
    """

    left: str | int = Field(min_length=1)
    right: str | int = Field(min_length=1)
    keys: tuple[JoinKey, ...] = Field(default=(), max_length=32)
    left_on: Optional[str] = Field(default=None, min_length=1, max_length=256)
    right_on: Optional[str] = Field(default=None, min_length=1, max_length=256)
    strategy: JoinStrategy = JoinStrategy.EXACT
    join_type: JoinType = JoinType.INNER
    threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("left", "right")
    @classmethod
    def validate_ref(cls, value: str | int) -> str | int:
        if isinstance(value, str) and not value.strip():
            raise ValueError("join references cannot be blank")
        if isinstance(value, int) and value < 0:
            raise ValueError("join index references cannot be negative")
        return value

    @model_validator(mode="after")
    def validate_keys(self) -> "JoinStep":
        if self.left == self.right:
            raise ValueError("a join step must reference two different inputs")
        legacy_present = self.left_on is not None or self.right_on is not None
        if legacy_present and (self.left_on is None or self.right_on is None):
            raise ValueError("left_on and right_on must be provided together")
        if legacy_present and self.keys:
            raise ValueError("use keys or left_on/right_on, not both")
        if not self.keys and not legacy_present:
            raise ValueError("a join step needs keys or left_on/right_on")
        if self.strategy == JoinStrategy.EXACT and self.threshold is not None:
            raise ValueError("threshold is only valid for similarity strategies")
        return self

    @property
    def key_pairs(self) -> tuple[JoinKey, ...]:
        if self.keys:
            return self.keys
        assert self.left_on is not None and self.right_on is not None
        return (JoinKey(left=self.left_on, right=self.right_on),)


class JoinSpec(_JoinModel):
    """Versioned, connector-neutral join intent."""

    schema_version: Literal[1] = JOIN_SCHEMA_VERSION
    inputs: tuple[JoinInput, ...] = Field(min_length=2, max_length=20)
    steps: tuple[JoinStep, ...] = Field(min_length=1, max_length=19)

    @model_validator(mode="after")
    def validate_plan_references(self) -> "JoinSpec":
        source_ids = [item.source_id for item in self.inputs]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("join input source_id values must be unique")
        known = set(source_ids)
        referenced: set[str] = set()
        for index, step in enumerate(self.steps):
            for ref in (step.left, step.right):
                if isinstance(ref, int):
                    if ref >= len(source_ids):
                        raise ValueError(f"join step {index} references unknown input index {ref}")
                    referenced.add(source_ids[ref])
                    continue
                if ref in known:
                    referenced.add(ref)
                    continue
                if ref.startswith("_result_"):
                    suffix = ref.removeprefix("_result_")
                    if not suffix.isdigit() or int(suffix) >= index:
                        raise ValueError(
                            f"join step {index} references an unavailable result '{ref}'"
                        )
                    continue
                raise ValueError(f"join step {index} references unknown input '{ref}'")
        if referenced != known:
            missing = ", ".join(sorted(known - referenced))
            raise ValueError(f"join plan does not connect inputs: {missing}")
        return self


class JoinRuntimeInput(_JoinModel):
    """Credential-free runtime facts used to resolve a ``JoinSpec``."""

    source_id: str = Field(min_length=1, max_length=512)
    source_backed: bool = False
    source_key: Optional[str] = Field(default=None, max_length=256)
    scope: DataScope = DataScope.FULL
    freshness: DataFreshness = DataFreshness.SNAPSHOT

    @model_validator(mode="after")
    def require_source_identity(self) -> "JoinRuntimeInput":
        if self.source_backed and not self.source_key:
            raise ValueError("source-backed inputs require an opaque source_key")
        return self


class JoinStrategyMetadata(_JoinModel):
    """Resolved capability and execution facts for one strategy."""

    strategy: JoinStrategy
    available: bool
    scope: Optional[DataScope] = None
    freshness: Optional[DataFreshness] = None
    executor: Optional[Executor] = None
    egress: JoinEgress = JoinEgress.NONE
    result_mode: Optional[JoinResultMode] = None
    reason_code: Optional[str] = None
    caveats: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_resolution(self) -> "JoinStrategyMetadata":
        if self.available:
            if not all((self.scope, self.freshness, self.executor, self.result_mode)):
                raise ValueError("available strategies require execution metadata")
            if not self.reason_code and self.executor is None:
                raise ValueError("available strategies require an executor")
        elif not self.reason_code:
            raise ValueError("unavailable strategies require reason_code")
        return self


class ResolvedJoinStep(_JoinModel):
    step: JoinStep
    strategy: JoinStrategyMetadata


class ResolvedJoinPlan(_JoinModel):
    """A deterministic execution decision, safe to show to user and agent."""

    schema_version: Literal[1] = JOIN_SCHEMA_VERSION
    spec: JoinSpec
    steps: tuple[ResolvedJoinStep, ...]
    available: bool
    scope: Optional[DataScope] = None
    freshness: Optional[DataFreshness] = None
    executor: Optional[Executor] = None
    egress: JoinEgress = JoinEgress.NONE
    result_mode: Optional[JoinResultMode] = None
    reason_code: Optional[str] = None
    caveats: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_step_alignment(self) -> "ResolvedJoinPlan":
        if len(self.steps) != len(self.spec.steps):
            raise ValueError("resolved steps must align one-to-one with JoinSpec steps")
        if self.available and not all(
            (self.scope, self.freshness, self.executor, self.result_mode)
        ):
            raise ValueError("available plans require execution metadata")
        if not self.available and not self.reason_code:
            raise ValueError("unavailable plans require reason_code")
        return self


_SCOPE_RANK = {DataScope.FULL: 0, DataScope.BOUNDED: 1, DataScope.SAMPLE: 2}

CROSS_SOURCE_UNAVAILABLE = "cross_source_unavailable"
CROSS_SOURCE_UNAVAILABLE_MESSAGE = (
    "Joining a live database with a file or another database isn't available yet. "
    "Join tables that live in the same database, or join uploaded files with each other."
)


def _strategy_metadata(
    strategy: JoinStrategy,
    *,
    source_side: bool,
    scope: DataScope,
    freshness: DataFreshness,
    mixed_sources: bool,
) -> JoinStrategyMetadata:
    if mixed_sources:
        return JoinStrategyMetadata(
            strategy=strategy,
            available=False,
            reason_code=CROSS_SOURCE_UNAVAILABLE,
            caveats=(CROSS_SOURCE_UNAVAILABLE_MESSAGE,),
        )
    fuzzy = strategy != JoinStrategy.EXACT
    executor = Executor.PANDAS if fuzzy else Executor.DUCKDB
    result_mode = JoinResultMode.PANDAS_MATERIALIZED if fuzzy else JoinResultMode.DUCKDB_SNAPSHOT
    egress = (
        JoinEgress.FULL if mixed_sources or strategy == JoinStrategy.SEMANTIC else JoinEgress.NONE
    )
    if source_side and strategy == JoinStrategy.EXACT:
        executor = Executor.SOURCE
        result_mode = JoinResultMode.SOURCE_SCRATCH
        egress = JoinEgress.PREVIEW_ONLY
        scope = DataScope.FULL
        freshness = DataFreshness.LIVE
    elif source_side:
        # Existing lexical matchers currently execute in Stych.  They must
        # stream complete inputs and create an explicit snapshot; the attached
        # planning preview is never an execution fallback.
        executor = Executor.PANDAS
        result_mode = JoinResultMode.PANDAS_MATERIALIZED
        egress = JoinEgress.FULL
        scope = DataScope.FULL
        freshness = DataFreshness.SNAPSHOT
    caveats_list: list[str] = []
    if strategy == JoinStrategy.SEMANTIC:
        caveats_list.append("Semantic matching sends values to the configured embedding service.")
    if source_side and fuzzy:
        caveats_list.append(
            "Complete source inputs are streamed into a session snapshot for matching."
        )
    return JoinStrategyMetadata(
        strategy=strategy,
        available=True,
        scope=scope,
        freshness=freshness,
        executor=executor,
        egress=egress,
        result_mode=result_mode,
        caveats=tuple(caveats_list),
    )


def resolve_join_plan(
    spec: JoinSpec, runtime_inputs: Sequence[JoinRuntimeInput]
) -> ResolvedJoinPlan:
    """Resolve current exact/file-fuzzy behavior without connector routing.

    Source-side execution is selected only when every input is source-backed
    and all opaque source identities match. Any other shape is materialized;
    callers can inspect the returned scope/egress metadata before executing.
    """

    by_id: Mapping[str, JoinRuntimeInput] = {item.source_id: item for item in runtime_inputs}
    expected = {item.source_id for item in spec.inputs}
    if set(by_id) != expected or len(by_id) != len(runtime_inputs):
        raise ValueError("runtime inputs must match JoinSpec inputs exactly")
    facts = [by_id[item.source_id] for item in spec.inputs]
    source_side = bool(facts) and all(item.source_backed for item in facts)
    source_side = source_side and len({item.source_key for item in facts}) == 1
    mixed_sources = not source_side and any(item.source_backed for item in facts)
    scope = max((item.scope for item in facts), key=_SCOPE_RANK.__getitem__)
    freshness = (
        DataFreshness.SNAPSHOT
        if mixed_sources or any(item.freshness == DataFreshness.SNAPSHOT for item in facts)
        else DataFreshness.LIVE
    )
    resolved_steps = tuple(
        ResolvedJoinStep(
            step=step,
            strategy=_strategy_metadata(
                step.strategy,
                source_side=source_side,
                scope=scope,
                freshness=freshness,
                mixed_sources=mixed_sources,
            ),
        )
        for step in spec.steps
    )
    blocked = next((item.strategy for item in resolved_steps if not item.strategy.available), None)
    if blocked is not None:
        return ResolvedJoinPlan(
            spec=spec,
            steps=resolved_steps,
            available=False,
            reason_code=blocked.reason_code,
            caveats=tuple(c for item in resolved_steps for c in item.strategy.caveats),
        )
    strategies = [item.strategy for item in resolved_steps]
    if source_side and all(item.step.strategy == JoinStrategy.EXACT for item in resolved_steps):
        executor = Executor.SOURCE
        result_mode = JoinResultMode.SOURCE_SCRATCH
        egress = JoinEgress.PREVIEW_ONLY
        scope = DataScope.FULL
        freshness = DataFreshness.LIVE
    elif any(item.strategy != JoinStrategy.EXACT for item in strategies):
        executor = Executor.PANDAS
        result_mode = JoinResultMode.PANDAS_MATERIALIZED
        egress = JoinEgress.FULL if mixed_sources or source_side else JoinEgress.NONE
        if source_side:
            freshness = DataFreshness.SNAPSHOT
    else:
        executor = Executor.DUCKDB
        result_mode = JoinResultMode.DUCKDB_SNAPSHOT
        egress = JoinEgress.FULL if mixed_sources else JoinEgress.NONE
    caveats = tuple(c for item in strategies for c in item.caveats)
    return ResolvedJoinPlan(
        spec=spec,
        steps=resolved_steps,
        available=True,
        scope=scope,
        freshness=freshness,
        executor=executor,
        egress=egress,
        result_mode=result_mode,
        caveats=caveats,
    )


__all__ = [
    "JOIN_SCHEMA_VERSION",
    "JoinEgress",
    "JoinInput",
    "JoinKey",
    "JoinResultMode",
    "JoinRuntimeInput",
    "JoinSpec",
    "JoinStep",
    "JoinStrategy",
    "JoinStrategyMetadata",
    "JoinType",
    "ResolvedJoinPlan",
    "ResolvedJoinStep",
    "resolve_join_plan",
]

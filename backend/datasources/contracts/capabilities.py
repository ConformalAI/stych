"""Shared capability vocabulary for every Stych data connector."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Capability(str, Enum):
    DISCOVER_NAMESPACES = "discover_namespaces"
    DISCOVER_RELATIONS = "discover_relations"
    DESCRIBE_SCHEMA = "describe_schema"
    PREVIEW_PAGE = "preview_page"
    EXACT_COUNT = "exact_count"
    COLUMN_STATS = "column_stats"
    FULL_SCAN = "full_scan"
    STREAM_ARROW = "stream_arrow"
    EXECUTE_NATIVE_QUERY = "execute_native_query"
    ESTIMATE_QUERY = "estimate_query"
    CANCEL_QUERY = "cancel_query"
    PROJECTION_PUSHDOWN = "projection_pushdown"
    PREDICATE_PUSHDOWN = "predicate_pushdown"
    SORT_PUSHDOWN = "sort_pushdown"
    LIMIT_PUSHDOWN = "limit_pushdown"
    AGGREGATION = "aggregation"
    SAME_SOURCE_JOIN = "same_source_join"
    CREATE_WORKING_COPY = "create_working_copy"
    MUTATE_WORKING_COPY = "mutate_working_copy"
    WRITE_BACK = "write_back"
    EXPORT_SNAPSHOT = "export_snapshot"
    INCREMENTAL_READ = "incremental_read"
    CDC = "cdc"
    STABLE_ROW_IDENTITY = "stable_row_identity"
    NESTED_VALUES = "nested_values"
    SCHEMA_COMPLETE = "schema_is_complete"


class SupportMode(str, Enum):
    NATIVE = "native"
    EMULATED = "emulated"
    UNSUPPORTED = "unsupported"


class DataScope(str, Enum):
    FULL = "full"
    BOUNDED = "bounded"
    SAMPLE = "sample"


class DataFreshness(str, Enum):
    LIVE = "live"
    SNAPSHOT = "snapshot"


class Executor(str, Enum):
    SOURCE = "source"
    PANDAS = "pandas"
    DUCKDB = "duckdb"
    HYBRID = "hybrid"


class JoinStrategySupport(BaseModel):
    """Execution facts for one supported same-source join strategy.

    ``CapabilitySupport`` describes the aggregate primitive, but a join has
    more than one execution path: exact joins can stay in a source while the
    current lexical strategies require a full materialization. Keeping those
    facts typed here prevents a top-level ``same_source_join`` declaration from
    hiding that distinction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str = Field(min_length=1, max_length=64)
    mode: SupportMode
    scope: DataScope
    freshness: DataFreshness
    executor: Executor
    egress: Literal["none", "preview_only", "full"]
    result_mode: Literal["source_scratch", "duckdb_snapshot", "pandas_materialized"]
    caveats: tuple[str, ...] = ()


class SameSourceJoinCapability(BaseModel):
    """Connector-specific same-source join matrix.

    The list is intentionally limited to strategies Stych actually exposes;
    upstream database features are not implied by this contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategies: tuple[JoinStrategySupport, ...] = Field(min_length=1)
    join_types: tuple[Literal["INNER", "LEFT", "RIGHT", "FULL OUTER"], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_strategy_matrix(self) -> "SameSourceJoinCapability":
        names = [item.strategy for item in self.strategies]
        if len(names) != len(set(names)):
            raise ValueError("same-source join strategies must be unique")
        if "exact" not in names:
            raise ValueError("same-source join capabilities must declare exact")
        return self


class ConnectorMaturity(str, Enum):
    PRODUCTION = "production"
    BETA = "beta"
    EXPERIMENTAL = "experimental"


class VerificationStatus(str, Enum):
    DECLARED = "declared"
    CONTRACT_TESTED = "contract_tested"
    INTEGRATION_TESTED = "integration_tested"


class CapabilitySupport(BaseModel):
    """How one connector currently delivers one primitive capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: SupportMode
    scope: Optional[DataScope] = None
    freshness: Optional[DataFreshness] = None
    executor: Optional[Executor] = None
    reason_code: Optional[str] = None
    limits: Dict[str, Any] = Field(default_factory=dict)
    caveats: tuple[str, ...] = ()
    join: Optional[SameSourceJoinCapability] = None

    @model_validator(mode="after")
    def validate_support_shape(self) -> "CapabilitySupport":
        if self.mode == SupportMode.UNSUPPORTED:
            if not self.reason_code:
                raise ValueError("unsupported capabilities require a reason_code")
            if self.scope or self.freshness or self.executor:
                raise ValueError("unsupported capabilities cannot declare execution semantics")
            if self.join is not None:
                raise ValueError("unsupported capabilities cannot declare join semantics")
        elif not all((self.scope, self.freshness, self.executor)):
            raise ValueError("supported capabilities require scope, freshness, and executor")
        return self


class ConnectorManifest(BaseModel):
    """Static, credential-free description of a connector implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    display_name: str
    category: str
    version: str
    maturity: ConnectorMaturity
    verification: VerificationStatus = VerificationStatus.DECLARED
    user_summary: str
    capabilities: Dict[Capability, CapabilitySupport]

    @model_validator(mode="after")
    def require_complete_capability_map(self) -> "ConnectorManifest":
        missing = set(Capability) - set(self.capabilities)
        extra = set(self.capabilities) - set(Capability)
        if missing or extra:
            raise ValueError(
                f"capability map must be exhaustive (missing={sorted(m.value for m in missing)}, "
                f"extra={sorted(str(e) for e in extra)})"
            )
        return self


class ProductFeature(str, Enum):
    PREVIEW = "preview"
    PROFILE = "profile"
    ANALYZE = "analyze"
    VISUALIZE = "visualize"
    FILTER = "filter"
    CLEAN = "clean"
    DIFF = "diff"
    EXPORT = "export"
    SAME_SOURCE_JOIN = "same_source_join"
    CROSS_SOURCE_JOIN = "cross_source_join"


class FeatureAvailability(str, Enum):
    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class FeatureDecision(BaseModel):
    """Resolved product behavior derived from primitive connector support."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature: ProductFeature
    availability: FeatureAvailability
    execution_path: Optional[Executor] = None
    scope: Optional[DataScope] = None
    freshness: Optional[DataFreshness] = None
    reason_code: Optional[str] = None
    user_message: str

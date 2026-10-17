"""Map connector primitives to product features in one auditable place."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    Capability,
    ConnectorManifest,
    DataFreshness,
    DataScope,
    Executor,
    FeatureAvailability,
    FeatureDecision,
    ProductFeature,
    SupportMode,
)
from .registry import get_manifest


@dataclass(frozen=True)
class _FeatureRule:
    required: tuple[Capability, ...]
    available_message: str


_RULES = {
    ProductFeature.PREVIEW: _FeatureRule((Capability.PREVIEW_PAGE,), "Preview is available."),
    ProductFeature.PROFILE: _FeatureRule((Capability.FULL_SCAN,), "Profiling is available."),
    ProductFeature.ANALYZE: _FeatureRule((Capability.FULL_SCAN,), "Analysis is available."),
    ProductFeature.VISUALIZE: _FeatureRule(
        (Capability.AGGREGATION,), "Visualization is available."
    ),
    ProductFeature.FILTER: _FeatureRule(
        (Capability.PREDICATE_PUSHDOWN, Capability.STABLE_ROW_IDENTITY),
        "Persistent filters are available.",
    ),
    ProductFeature.CLEAN: _FeatureRule((Capability.MUTATE_WORKING_COPY,), "Cleaning is available."),
    ProductFeature.DIFF: _FeatureRule(
        (Capability.FULL_SCAN, Capability.STABLE_ROW_IDENTITY), "Diff is available."
    ),
    ProductFeature.EXPORT: _FeatureRule((Capability.EXPORT_SNAPSHOT,), "Export is available."),
    ProductFeature.SAME_SOURCE_JOIN: _FeatureRule(
        (Capability.SAME_SOURCE_JOIN,), "Same-source joins are available."
    ),
    ProductFeature.CROSS_SOURCE_JOIN: _FeatureRule(
        (Capability.FULL_SCAN, Capability.EXPORT_SNAPSHOT),
        "Cross-source joins use materialized data.",
    ),
}

_SCOPE_RANK = {DataScope.FULL: 0, DataScope.BOUNDED: 1, DataScope.SAMPLE: 2}


def _execution_path(executors: set[Executor]) -> Executor:
    return next(iter(executors)) if len(executors) == 1 else Executor.HYBRID


def resolve_feature(manifest: ConnectorManifest, feature: ProductFeature) -> FeatureDecision:
    # Live database rows stay in the customer's system. Mixed file+warehouse
    # and warehouse-to-warehouse joins are not offered until a same-engine
    # path exists that does not copy data into Stych.
    if feature == ProductFeature.CROSS_SOURCE_JOIN:
        return FeatureDecision(
            feature=feature,
            availability=FeatureAvailability.UNAVAILABLE,
            reason_code="cross_source_unavailable",
            user_message=(
                "Joining a live database with a file or another database isn't available yet. "
                "Join tables that live in the same database, or join uploaded files with each other."
            ),
        )
    rule = _RULES[feature]
    support = [manifest.capabilities[capability] for capability in rule.required]
    missing = [
        capability
        for capability, item in zip(rule.required, support)
        if item.mode == SupportMode.UNSUPPORTED
    ]
    if missing:
        names = ", ".join(capability.value for capability in missing)
        return FeatureDecision(
            feature=feature,
            availability=FeatureAvailability.UNAVAILABLE,
            reason_code=f"missing:{','.join(c.value for c in missing)}",
            user_message=f"Unavailable because {names} is not supported.",
        )

    scope = max((item.scope for item in support if item.scope), key=_SCOPE_RANK.__getitem__)
    freshness = (
        DataFreshness.SNAPSHOT
        if any(item.freshness == DataFreshness.SNAPSHOT for item in support)
        else DataFreshness.LIVE
    )
    executors = {item.executor for item in support if item.executor}
    limited = scope != DataScope.FULL
    if limited:
        limit = next((item.limits.get("rows") for item in support if item.limits.get("rows")), None)
        suffix = f" (up to {limit:,} rows)." if isinstance(limit, int) else "."
        message = f"Available with {scope.value} data{suffix}"
    else:
        message = rule.available_message
    return FeatureDecision(
        feature=feature,
        availability=(FeatureAvailability.LIMITED if limited else FeatureAvailability.AVAILABLE),
        execution_path=_execution_path(executors),
        scope=scope,
        freshness=freshness,
        reason_code="bounded_data" if limited else None,
        user_message=message,
    )


def resolve_features(connector_id: str) -> dict[ProductFeature, FeatureDecision]:
    manifest = get_manifest(connector_id)
    return {feature: resolve_feature(manifest, feature) for feature in ProductFeature}


def capability_summary(
    connector_id: str,
    *,
    sampled_fallback: bool = False,
    working_copy: bool = False,
) -> str:
    """Short end-user summary. Detailed capability data stays in generated docs."""
    try:
        manifest = get_manifest(connector_id)
    except KeyError:
        return "Snapshot · Current loaded data"
    if working_copy:
        return "Working copy · Full-data analysis"
    if sampled_fallback and connector_id != "upload":
        unit = "documents" if connector_id == "mongodb" else "rows"
        return f"Snapshot · Loaded sample (up to 10K {unit})"
    return manifest.user_summary

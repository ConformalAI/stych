from __future__ import annotations

import pytest
from pydantic import ValidationError

from datasources.contracts import (
    DataFreshness,
    DataScope,
    Executor,
    JoinEgress,
    JoinInput,
    JoinResultMode,
    JoinRuntimeInput,
    JoinSpec,
    JoinStep,
    JoinStrategy,
    JoinType,
    resolve_join_plan,
)


def _spec(*strategies: JoinStrategy) -> JoinSpec:
    steps = tuple(
        JoinStep(
            left="a" if index == 0 else "_result_0",
            right="b" if index == 0 else "c",
            left_on="id",
            right_on="id",
            strategy=strategy,
            join_type=JoinType.LEFT,
            threshold=0.8 if strategy != JoinStrategy.EXACT else None,
        )
        for index, strategy in enumerate(strategies)
    )
    inputs = (JoinInput(source_id="a"), JoinInput(source_id="b"))
    if len(strategies) > 1:
        inputs += (JoinInput(source_id="c"),)
    return JoinSpec(inputs=inputs, steps=steps)


def test_join_spec_is_versioned_and_canonicalizes_legacy_keys() -> None:
    spec = _spec(JoinStrategy.EXACT)
    assert spec.schema_version == 1
    assert spec.steps[0].key_pairs[0].left == "id"
    assert spec.steps[0].key_pairs[0].right == "id"
    assert spec.steps[0].join_type == JoinType.LEFT


def test_join_spec_supports_multi_key_steps() -> None:
    spec = JoinSpec(
        inputs=(JoinInput(source_id="a"), JoinInput(source_id="b")),
        steps=(
            JoinStep(
                left="a",
                right="b",
                keys=(
                    {"left": "tenant_id", "right": "tenant_id"},
                    {"left": "customer_id", "right": "customer_id"},
                ),
            ),
        ),
    )
    assert [pair.left for pair in spec.steps[0].key_pairs] == ["tenant_id", "customer_id"]


def test_join_spec_rejects_ambiguous_or_disconnected_intent() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        JoinSpec.model_validate(
            {
                "schema_version": 2,
                "inputs": [{"source_id": "a"}, {"source_id": "b"}],
                "steps": [{"left": "a", "right": "b", "left_on": "id", "right_on": "id"}],
            }
        )
    with pytest.raises(ValidationError, match="left_on and right_on must be provided together"):
        JoinStep(left="a", right="b", left_on="id")
    with pytest.raises(ValidationError, match="does not connect inputs"):
        JoinSpec(
            inputs=(JoinInput(source_id="a"), JoinInput(source_id="b"), JoinInput(source_id="c")),
            steps=(JoinStep(left="a", right="b", left_on="id", right_on="id"),),
        )


def test_all_existing_file_strategies_are_typed() -> None:
    for strategy in JoinStrategy:
        if strategy == JoinStrategy.EXACT:
            step = JoinStep(left="a", right="b", left_on="id", right_on="id", strategy=strategy)
        else:
            step = JoinStep(
                left="a",
                right="b",
                left_on="name",
                right_on="name",
                strategy=strategy,
                threshold=0.8,
            )
        assert step.strategy is strategy


def test_same_source_exact_resolves_to_live_source_scratch() -> None:
    spec = _spec(JoinStrategy.EXACT)
    plan = resolve_join_plan(
        spec,
        [
            JoinRuntimeInput(
                source_id="a",
                source_backed=True,
                source_key="postgres:one",
                scope=DataScope.FULL,
                freshness=DataFreshness.LIVE,
            ),
            JoinRuntimeInput(
                source_id="b",
                source_backed=True,
                source_key="postgres:one",
                scope=DataScope.FULL,
                freshness=DataFreshness.LIVE,
            ),
        ],
    )
    assert plan.available is True
    assert plan.executor is Executor.SOURCE
    assert plan.scope is DataScope.FULL
    assert plan.freshness is DataFreshness.LIVE
    assert plan.egress is JoinEgress.PREVIEW_ONLY
    assert plan.result_mode is JoinResultMode.SOURCE_SCRATCH


def test_same_source_fuzzy_uses_full_stream_not_sample_fallback() -> None:
    spec = _spec(JoinStrategy.LEVENSHTEIN)
    plan = resolve_join_plan(
        spec,
        [
            JoinRuntimeInput(source_id="a", source_backed=True, source_key="pg:one"),
            JoinRuntimeInput(source_id="b", source_backed=True, source_key="pg:one"),
        ],
    )
    assert plan.available is True
    assert plan.scope is DataScope.FULL
    assert plan.freshness is DataFreshness.SNAPSHOT
    assert plan.executor is Executor.PANDAS
    assert plan.egress is JoinEgress.FULL
    assert "complete source inputs" in " ".join(plan.caveats).lower()


def test_mixed_sources_are_unavailable() -> None:
    spec = _spec(JoinStrategy.EXACT)
    plan = resolve_join_plan(
        spec,
        [
            JoinRuntimeInput(source_id="a", source_backed=False),
            JoinRuntimeInput(
                source_id="b",
                source_backed=True,
                source_key="mongo:one",
                scope=DataScope.SAMPLE,
                freshness=DataFreshness.SNAPSHOT,
            ),
        ],
    )
    assert plan.available is False
    assert plan.reason_code == "cross_source_unavailable"

from types import SimpleNamespace

import pandas as pd
import pytest

from agents.linking.plan_validation import (
    AgentJoinPlanError,
    resolve_agent_join_plan,
    validate_agent_join_plan,
)
from datasources.contracts import DataFreshness, DataScope, Executor, JoinEgress


def _ctx(name: str, *, source: str = "upload"):
    metadata = {"source": source}
    if source == "postgres":
        metadata.update(
            connection_url="postgresql://user:pw@localhost/analytics",
            schema="public",
            table=name,
        )
    frame = pd.DataFrame({"id": [1], "label": [name]})
    return SimpleNamespace(
        file_id=name,
        filename=name,
        working_dataframe=frame,
        dataframe=frame,
        metadata=metadata,
    )


def _step(**changes):
    step = {
        "left": "a",
        "right": "b",
        "left_on": "id",
        "right_on": "id",
        "strategy": "exact",
        "join_type": "LEFT",
    }
    step.update(changes)
    return step


def test_agent_plan_is_normalized_and_typed_without_taking_strategy_choice_away():
    normalized, spec = validate_agent_join_plan([_ctx("a"), _ctx("b")], [_step()])
    assert normalized == [_step()]
    assert spec.steps[0].strategy.value == "exact"
    assert spec.steps[0].join_type.value == "LEFT"


@pytest.mark.parametrize(
    "step, message",
    [
        (_step(right="invented"), "unknown input"),
        (_step(left_on="invented"), "missing left key"),
        (_step(strategy="semantic"), "parity scope"),
        (_step(join_type="CROSS"), "invalid"),
    ],
)
def test_agent_plan_fails_closed(step, message):
    with pytest.raises(AgentJoinPlanError, match=message):
        validate_agent_join_plan([_ctx("a"), _ctx("b")], [step])


def test_same_source_lexical_plan_is_full_snapshot_with_explicit_egress():
    contexts = [_ctx("a", source="postgres"), _ctx("b", source="postgres")]
    _, spec = validate_agent_join_plan(contexts, [_step(strategy="levenshtein", threshold=0.8)])
    resolved = resolve_agent_join_plan(contexts, spec)
    assert resolved.available
    assert resolved.scope is DataScope.FULL
    assert resolved.freshness is DataFreshness.SNAPSHOT
    assert resolved.executor is Executor.PANDAS
    assert resolved.egress is JoinEgress.FULL


def test_multi_step_plan_must_extend_one_accumulated_result():
    contexts = [_ctx("a"), _ctx("b"), _ctx("c"), _ctx("d")]
    normalized, _ = validate_agent_join_plan(
        contexts,
        [
            _step(),
            _step(left="a", right="c"),
            _step(left="b", right="d"),
        ],
    )
    assert [step["right"] for step in normalized] == ["b", "c", "d"]

    with pytest.raises(AgentJoinPlanError, match="exactly one new dataset"):
        validate_agent_join_plan(
            contexts,
            [_step(), _step(left="c", right="d")],
        )

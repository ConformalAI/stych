from types import SimpleNamespace

import pandas as pd
import pytest

from agents.linking.agent import DataLinkingAgent


@pytest.mark.asyncio
async def test_materialized_star_plan_accumulates_prior_result():
    agent = DataLinkingAgent(SimpleNamespace())
    datasets = [
        {"name": "a", "data": pd.DataFrame({"id": [1], "a_value": ["a"]})},
        {"name": "b", "data": pd.DataFrame({"id": [1], "b_value": ["b"]})},
        {"name": "c", "data": pd.DataFrame({"id": [1], "c_value": ["c"]})},
    ]
    plan = [
        {
            "left": "a",
            "right": "b",
            "left_on": "id",
            "right_on": "id",
            "strategy": "exact",
            "join_type": "INNER",
        },
        {
            "left": "a",
            "right": "c",
            "left_on": "id",
            "right_on": "id",
            "strategy": "exact",
            "join_type": "INNER",
        },
    ]

    result = await agent.multi_join(datasets=datasets, join_plan=plan)

    assert "error" not in result
    assert result["rows"] == 1
    assert set(result["columns"]) == {"id", "a_value", "b_value", "c_value"}


@pytest.mark.asyncio
async def test_materialized_plan_refuses_disconnected_second_step():
    agent = DataLinkingAgent(SimpleNamespace())
    datasets = [{"name": name, "data": pd.DataFrame({"id": [1]})} for name in ("a", "b", "c", "d")]
    result = await agent.multi_join(
        datasets=datasets,
        join_plan=[
            {"left": "a", "right": "b", "left_on": "id", "right_on": "id"},
            {"left": "c", "right": "d", "left_on": "id", "right_on": "id"},
        ],
    )

    assert "exactly one new dataset" in result["error"]

"""Parity of the existing fuzzy strategies across supported join types."""

from __future__ import annotations

import pandas as pd
import pytest

from agents.linking.agent import DataLinkingAgent
from tools.linking_fuzzy_tools import FuzzyLinkingTools

LEFT = pd.DataFrame({"name": ["alpha", "left only"], "left_value": [1, 2]})
RIGHT = pd.DataFrame({"name": ["alfa", "right only"], "right_value": [10, 20]})
MATCHES = [{"left_idx": 0, "right_idx": 0, "score": 0.9}]


@pytest.mark.parametrize(
    ("join_type", "expected_rows"),
    [("inner", 1), ("left", 2), ("right", 2), ("full outer", 3)],
)
def test_fuzzy_join_type_preserves_the_expected_unmatched_sides(join_type, expected_rows):
    result, _ = FuzzyLinkingTools._build_result(LEFT, RIGHT, MATCHES, "name", "name", join_type)

    assert len(result) == expected_rows
    assert list(result.columns) == [
        "name",
        "left_value",
        "right_name",
        "right_value",
        "_match_score",
    ]


def test_fuzzy_join_keeps_both_different_key_values():
    result, _ = FuzzyLinkingTools._build_result(LEFT, RIGHT, MATCHES, "name", "name", "inner")

    assert result.loc[0, "name"] == "alpha"
    assert result.loc[0, "right_name"] == "alfa"


def test_fuzzy_full_outer_includes_each_unmatched_side_once():
    result, _ = FuzzyLinkingTools._build_result(LEFT, RIGHT, MATCHES, "name", "name", "full outer")

    assert result["name"].iloc[:2].tolist() == ["alpha", "left only"]
    assert pd.isna(result["name"].iloc[2])
    assert result["right_name"].iloc[[0, 2]].tolist() == ["alfa", "right only"]
    assert pd.isna(result["right_name"].iloc[1])


def test_empty_fuzzy_inner_join_keeps_a_stable_schema():
    result, _ = FuzzyLinkingTools._build_result(LEFT, RIGHT, [], "name", "name", "inner")

    assert result.empty
    assert list(result.columns) == [
        "name",
        "left_value",
        "right_name",
        "right_value",
        "_match_score",
    ]


def test_unknown_fuzzy_join_type_is_rejected():
    with pytest.raises(ValueError, match="Unsupported fuzzy join type"):
        FuzzyLinkingTools._build_result(LEFT, RIGHT, MATCHES, "name", "name", "cross")


@pytest.mark.asyncio
async def test_typed_single_key_fuzzy_plan_executes_after_normalization():
    agent = DataLinkingAgent(None)
    result = await agent.multi_join(
        datasets=[
            {"name": "left", "data": LEFT},
            {"name": "right", "data": RIGHT},
        ],
        join_plan=[
            {
                "left": "left",
                "right": "right",
                "keys": [{"left": "name", "right": "name"}],
                "strategy": "levenshtein",
                "threshold": 0.5,
                "join_type": "INNER",
            }
        ],
    )

    assert "error" not in result
    assert result["rows"] > 0

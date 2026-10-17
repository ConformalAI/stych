import numpy as np
import pandas as pd

from agents.linking.agent import DataLinkingAgent
from tools.linking_fuzzy_tools import FuzzyLinkingTools


def test_exact_inner_join_does_not_match_null_keys():
    left = pd.DataFrame({"id": [1, np.nan], "left_value": ["match", "null-left"]})
    right = pd.DataFrame({"id": [1, np.nan], "right_value": ["match", "null-right"]})

    result = DataLinkingAgent._merge_exact(left, right, "INNER", "id", "id")

    assert len(result) == 1
    assert result.iloc[0]["left_value"] == "match"


def test_exact_full_join_preserves_null_rows_without_matching_them():
    left = pd.DataFrame({"id": [np.nan], "left_value": ["null-left"]})
    right = pd.DataFrame({"id": [np.nan], "right_value": ["null-right"]})

    result = DataLinkingAgent._merge_exact(left, right, "FULL OUTER", "id", "id")

    assert len(result) == 2
    assert result["left_value"].notna().sum() == 1
    assert result["right_value"].notna().sum() == 1


def test_fuzzy_join_does_not_turn_nulls_into_matching_strings():
    left = pd.DataFrame({"name": [None, "Alice"]})
    right = pd.DataFrame({"name": [None, "Alice"]})

    result = FuzzyLinkingTools.fuzzy_join(
        left,
        right,
        left_on="name",
        right_on="name",
        strategy="levenshtein",
        threshold=1.0,
        join_type="inner",
    )

    assert result["rows"] == 1
    assert result["statistics"]["matched_rows"] == 1

"""Tests for linking candidate scores and the inspect report."""

from datetime import datetime

import pandas as pd
import pytest

from agents.linking.inspect import (
    MAX_CACHE_CANDIDATES,
    STORE_KEY,
    build_inspect_store,
    collect_candidates,
    inspect_source_row,
    public_context,
)
from agents.state import AgentState, DataContext
from tools.linking_fuzzy_tools import FuzzyLinkingTools


def _apple_frames():
    left = pd.DataFrame(
        {
            "company": ["Apple", "Google", "Microsoft"],
            "region": ["US", "US", "US"],
        }
    )
    right = pd.DataFrame(
        {
            "name": ["Apple Inc", "Apple Corp", "Apple Cor", "Alphabet Inc", "Microsoft Corp"],
            "ticker": ["AAPL", "APLC", "APCR", "GOOGL", "MSFT"],
        }
    )
    return left, right


def test_fuzzy_join_keeps_candidate_table_not_just_winner():
    left, right = _apple_frames()
    result = FuzzyLinkingTools.fuzzy_join(
        left,
        right,
        left_on="company",
        right_on="name",
        strategy="token_set",
        threshold=0.8,
        max_matches=1,
        join_type="inner",
        candidate_top_n=5,
        candidate_floor=0.3,
    )
    assert "error" not in result
    candidates = result["candidates"]
    apple_cands = [c for c in candidates if c["source_row_id"] == 0]
    assert len(apple_cands) >= 2
    assert {c["candidate_value"] for c in apple_cands} >= {"Apple Inc", "Apple Corp"}
    matched_left = result["statistics"]["matched_rows"]
    assert matched_left >= 1
    assert all("source_row_id" in c and "score" in c and "strategy" in c for c in apple_cands)


def test_join_statistics_use_thresholded_matches_not_near_misses():
    left, right = _apple_frames()
    result = FuzzyLinkingTools.fuzzy_join(
        left,
        right,
        left_on="company",
        right_on="name",
        strategy="token_set",
        threshold=0.99,
        max_matches=1,
        join_type="inner",
        candidate_top_n=5,
        candidate_floor=0.2,
    )
    assert "error" not in result
    assert len(result["candidates"]) >= len(result.get("match_details") or [])


def test_inspect_row_ranks_and_filters_by_threshold():
    left, right = _apple_frames()
    candidates = collect_candidates(
        left,
        right,
        "company",
        "name",
        "token_set",
        threshold=0.8,
        top_n=5,
        floor=0.2,
    )
    store = build_inspect_store(
        left,
        right,
        left_name="companies.csv",
        right_name="entities.csv",
        left_on="company",
        right_on="name",
        strategy="token_set",
        threshold=0.8,
        candidates=candidates,
        committed=False,
    )
    ctx = public_context(store)
    assert ctx is not None
    assert "candidates" not in ctx
    assert ctx["left"]["join_key"] == "company"
    assert ctx["has_lexical"] is True

    low = inspect_source_row(store, 0, strategy="lexical", threshold=0.2, top_n=5)
    high = inspect_source_row(store, 0, strategy="lexical", threshold=0.95, top_n=5)
    assert low["candidates"]
    assert low["candidates"][0]["score"] >= low["candidates"][-1]["score"]
    assert len(high["candidates"]) <= len(low["candidates"])
    assert all(c["score"] >= 0.95 for c in high["candidates"])
    assert "candidate_row_id" in low["candidates"][0]
    assert low["candidates"][0]["row"]["_row_id"] == low["candidates"][0]["candidate_row_id"]


def test_exact_join_candidates_score_one():
    left = pd.DataFrame({"id": ["A", "B"], "v": [1, 2]})
    right = pd.DataFrame({"id": ["A", "A", "C"], "w": [10, 11, 12]})
    candidates = collect_candidates(left, right, "id", "id", "exact", threshold=1.0, top_n=4)
    exact = [c for c in candidates if c["strategy"] == "exact" and c["source_row_id"] == 0]
    assert len(exact) == 2
    assert all(c["score"] == 1.0 for c in exact)


def test_right_cache_is_capped():
    left = pd.DataFrame({"company": [f"s{i}" for i in range(5)]})
    right = pd.DataFrame({"name": [f"c{i}" for i in range(MAX_CACHE_CANDIDATES + 50)]})
    bulky = []
    for src in range(5):
        for cand in range(MAX_CACHE_CANDIDATES + 50):
            bulky.append(
                {
                    "source_row_id": src,
                    "candidate_row_id": cand,
                    "score": 0.5,
                    "strategy": "token_set",
                    "source_value": f"s{src}",
                    "candidate_value": f"c{cand}",
                }
            )
    store = build_inspect_store(
        left,
        right,
        left_name="left.csv",
        right_name="right.csv",
        left_on="company",
        right_on="name",
        strategy="token_set",
        threshold=0.5,
        candidates=bulky,
    )
    assert len(store["right_row_cache"]) <= MAX_CACHE_CANDIDATES


def test_preview_inspect_does_not_mutate_frames():
    from agents.linking.agent import DataLinkingAgent

    class _Dummy:
        async def generate(self, request):
            raise RuntimeError("LLM should not be called")

    left, right = _apple_frames()
    left_copy = left.copy(deep=True)
    agent = DataLinkingAgent(_Dummy(), db_config={})
    result = agent.preview_inspect(
        datasets=[
            {"data": left, "name": "companies.csv"},
            {"data": right, "name": "entities.csv"},
        ],
        join_plan=[
            {
                "left": "companies.csv",
                "right": "entities.csv",
                "left_on": "company",
                "right_on": "name",
                "strategy": "token_set",
                "threshold": 0.7,
                "join_type": "INNER",
            }
        ],
        match_strategy="token_set",
        fuzzy_threshold=0.7,
    )
    assert "error" not in result
    assert result["inspect"]["candidates"]
    pd.testing.assert_frame_equal(left, left_copy)


@pytest.mark.asyncio
async def test_linking_confirm_cancel_clears_plan():
    from agents.orchestrator import subagent_calls as sc

    left, right = _apple_frames()
    state = AgentState(session_id="s-inspect")
    state.set_data_context(
        DataContext(
            file_id="f1",
            filename="companies.csv",
            raw_dataframe=left.copy(),
            working_dataframe=left.copy(),
            uploaded_at=datetime.now(),
        )
    )
    state.add_data_context(
        DataContext(
            file_id="f2",
            filename="entities.csv",
            raw_dataframe=right.copy(),
            working_dataframe=right.copy(),
            uploaded_at=datetime.now(),
        )
    )
    state.metadata["last_linking_plan"] = {
        "join_plan": [{"left": "companies.csv", "right": "entities.csv"}],
        "reasoning": "test",
    }
    state.metadata[STORE_KEY] = {"candidates": [], "context": {}}

    class _Orch:
        linking_agent = None

    result = await sc.subagent_linking(
        _Orch(),
        "",
        state,
        "",
        "s-inspect",
        None,
        user_action={"kind": "linking_confirm", "value": "decline"},
    )
    assert "did not execute" in result["response"].lower()
    assert "last_linking_plan" not in state.metadata
    assert STORE_KEY not in state.metadata
    pd.testing.assert_frame_equal(state.data_context.working_dataframe, left)

"""
Unit tests for the token-optimization caps: engine observation backstops,
absolute transcript budget, linking prompt clipping, MCP result capping,
deterministic formatter, and provider usage normalization.
"""

from __future__ import annotations

import pandas as pd
import pytest

from agents.formatter.agent import FormatterAgent
from agents.linking.agent import _PROMPT_SECTION_CHARS, _clip_json
from llm.models import Message, usage_to_dict
from react_engine.compaction import (
    CLEARED_TOOL_CONTENT,
    COMPACT_TO_FRACTION,
    DEFAULT_MAX_TRANSCRIPT_TOKENS,
    estimate_transcript_tokens,
    maybe_compact,
)
from react_engine.loop import (
    MAX_OBSERVATION_CHARS,
    MAX_STEP_OBSERVATIONS_CHARS,
    _clip_observation,
    _enforce_step_budget,
    transcript_budget_from_config,
)
from tools.mcp_tools import _MAX_MCP_RESULT_CHARS, _cap_mcp_result

# ---------------------------------------------------------------------------
# Engine observation backstops
# ---------------------------------------------------------------------------


def test_clip_observation_passthrough_and_cap():
    assert _clip_observation("short") == "short"
    big = "x" * (MAX_OBSERVATION_CHARS + 500)
    clipped = _clip_observation(big)
    assert len(clipped) < len(big)
    assert clipped.startswith("x" * 100)
    assert "truncated" in clipped


def test_enforce_step_budget_shrinks_largest_first():
    sibs = [
        {"tool_call_id": "a", "content": "x" * 6000},
        {"tool_call_id": "b", "content": "y" * 6000},
        {"tool_call_id": "c", "content": "z" * 6000},
        {"tool_call_id": "d", "content": "small"},
    ]
    _enforce_step_budget(sibs)
    total = sum(len(s["content"]) for s in sibs)
    # Budget plus the truncation-marker overhead added per clipped observation.
    assert total <= MAX_STEP_OBSERVATIONS_CHARS + 200
    # The small observation is untouched.
    assert sibs[3]["content"] == "small"


def test_enforce_step_budget_noop_under_budget():
    sibs = [{"tool_call_id": "a", "content": "hello"}]
    _enforce_step_budget(sibs)
    assert sibs[0]["content"] == "hello"


# ---------------------------------------------------------------------------
# Absolute transcript budget
# ---------------------------------------------------------------------------


def _big_transcript(n_steps: int, obs: str) -> list:
    transcript = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="task"),
    ]
    for i in range(n_steps):
        transcript.append(
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            )
        )
        transcript.append(Message(role="tool", tool_call_id=f"c{i}", content=obs))
    return transcript


def test_maybe_compact_clears_old_tool_results():
    # Mixed text so tiktoken doesn't compress it away.
    obs = ("row 4821 revenue 1532.75 category Beverages status ok " * 80)[:4000]
    transcript = _big_transcript(60, obs)

    out, compacted = maybe_compact(
        transcript, "openrouter/anthropic/claude-sonnet-4.5", max_transcript_tokens=40_000
    )
    assert compacted is True
    # Old tool results are cleared whole — an honest marker, not a garbled cut.
    assert out[3].content == CLEARED_TOOL_CONTENT
    # Recent ones kept verbatim.
    assert out[-1].content == obs
    # System + first user preserved verbatim.
    assert out[0].content == "system prompt"
    assert out[1].content == "task"


def test_maybe_compact_hysteresis_and_stability():
    obs = ("row 4821 revenue 1532.75 category Beverages status ok " * 80)[:4000]
    transcript = _big_transcript(60, obs)

    out, compacted = maybe_compact(transcript, "claude-sonnet-4.5", max_transcript_tokens=40_000)
    assert compacted is True
    # One pass compacts down to the low-water mark, not just barely under budget…
    assert estimate_transcript_tokens(out) <= int(40_000 * COMPACT_TO_FRACTION) + 2_000
    # …so the very next preflight is a no-op and the cached prefix stays stable.
    out2, compacted2 = maybe_compact(out, "claude-sonnet-4.5", max_transcript_tokens=40_000)
    assert compacted2 is False
    assert out2 is out


def test_default_budget_is_120k():
    assert DEFAULT_MAX_TRANSCRIPT_TOKENS == 120_000


def test_maybe_compact_respects_explicit_budget():
    transcript = [
        Message(role="system", content="s"),
        Message(role="user", content="u"),
    ]
    for i in range(10):
        transcript.append(Message(role="tool", tool_call_id=f"c{i}", content="o" * 4000))
    # Generous explicit budget: nothing should shrink.
    out, compacted = maybe_compact(
        transcript,
        "claude-sonnet-4.5",
        max_transcript_tokens=DEFAULT_MAX_TRANSCRIPT_TOKENS * 10,
    )
    assert compacted is False
    assert out is transcript


def test_transcript_budget_from_config():
    assert transcript_budget_from_config(None) is None
    assert transcript_budget_from_config({}) is None
    assert (
        transcript_budget_from_config({"llm": {"react_transcript_budget_tokens": 12345}}) == 12345
    )


# ---------------------------------------------------------------------------
# Linking prompt clipping
# ---------------------------------------------------------------------------


def test_clip_json_small_payload_untouched():
    assert _clip_json({"a": 1}) == '{"a": 1}'


def test_clip_json_caps_large_payload():
    huge = {"rows": ["v" * 100] * 2000}
    blob = _clip_json(huge)
    assert len(blob) <= _PROMPT_SECTION_CHARS + 60
    assert "truncated" in blob


# ---------------------------------------------------------------------------
# MCP result capping
# ---------------------------------------------------------------------------


def test_cap_mcp_result_small_passthrough():
    assert _cap_mcp_result("ok", "get_values", None) == "ok"


def test_cap_mcp_result_truncates_without_workspace():
    big = "r" * (_MAX_MCP_RESULT_CHARS + 10_000)
    capped = _cap_mcp_result(big, "get_values", None)
    assert len(capped) < len(big)
    assert "truncated" in capped


def test_cap_mcp_result_persists_to_workspace(tmp_path, monkeypatch):
    import tools.workspace_tools as wt

    monkeypatch.setattr(wt, "_get_workspace_path", lambda state: tmp_path)
    big = "r" * (_MAX_MCP_RESULT_CHARS + 10_000)
    capped = _cap_mcp_result(big, "get_values", object())
    assert "mcp_results/" in capped
    assert "Preview" in capped
    saved = list((tmp_path / "mcp_results").glob("*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text() == big


# ---------------------------------------------------------------------------
# Deterministic formatter (no LLM pass)
# ---------------------------------------------------------------------------


class _ExplodingRouter:
    """Any LLM use fails the test."""

    async def generate(self, request):  # pragma: no cover - must not be called
        raise AssertionError("FormatterAgent must not call the LLM")

    async def generate_stream(self, request):  # pragma: no cover - must not be called
        raise AssertionError("FormatterAgent must not call the LLM")


@pytest.fixture()
def formatter():
    return FormatterAgent(llm_router=_ExplodingRouter())


async def test_formatter_string_passthrough(formatter):
    assert await formatter.format_data("## Already markdown") == "## Already markdown"


async def test_formatter_dataframe_capped_at_20_rows(formatter):
    df = pd.DataFrame({"a": range(100), "b": range(100)})
    out = await formatter.format_data(df, question="Numbers")
    assert "**Numbers**" in out
    assert "Showing first 20 of 100 rows" in out
    # Row 21+ must not be rendered.
    assert "| 25 |" not in out


async def test_formatter_dict_with_nested_values(formatter):
    out = await formatter.format_data(
        {"status": "done", "shape": {"rows": 10, "cols": 3}}, question="Summary"
    )
    assert "**Summary**" in out
    assert "status" in out and "done" in out
    assert '"rows": 10' in out


# ---------------------------------------------------------------------------
# Provider usage normalization (cache accounting)
# ---------------------------------------------------------------------------


class _Details:
    cached_tokens = 900


class _Usage:
    prompt_tokens = 1000
    completion_tokens = 50
    total_tokens = 1050
    prompt_tokens_details = _Details()
    cache_creation_input_tokens = 100


def test_usage_to_dict_extracts_cache_fields():
    d = usage_to_dict(_Usage())
    assert d["prompt_tokens"] == 1000
    assert d["completion_tokens"] == 50
    assert d["cached_tokens"] == 900
    assert d["cache_creation_tokens"] == 100


def test_usage_to_dict_none():
    assert usage_to_dict(None) == {}


# ---------------------------------------------------------------------------
# Cache-aware cost estimation
# ---------------------------------------------------------------------------


def test_cost_estimate_discounts_cache_reads():
    from llm.usage_tracker import UsageCostEstimator

    est = UsageCostEstimator()
    full, _ = est.estimate("claude-sonnet-4.5", 10_000, 100)
    mostly_cached, _ = est.estimate("claude-sonnet-4.5", 10_000, 100, cached_tokens=9_000)
    # 9K of 10K at 0.1x: input cost drops from 10K units to 1K + 0.9K = 1.9K units.
    assert mostly_cached < full * 0.5
    # Cached tokens never exceed prompt tokens even if the provider misreports.
    clamped, _ = est.estimate("claude-sonnet-4.5", 1_000, 0, cached_tokens=5_000)
    floor, _ = est.estimate("claude-sonnet-4.5", 1_000, 0, cached_tokens=1_000)
    assert clamped == floor


def test_compute_billed_tokens():
    from llm.usage_tracker import compute_billed_tokens

    # No cache: billed == raw.
    assert compute_billed_tokens(1000, 100) == 1100
    # 90% cache reads: 100 + 900*0.1 + 100 = 290.
    assert compute_billed_tokens(1000, 100, cached_tokens=900) == 290
    # Cache write premium: 1000*1.25 + 100 = 1350.
    assert compute_billed_tokens(1000, 100, cache_creation_tokens=1000) == 1350
    # Provider misreport clamps to prompt size.
    assert compute_billed_tokens(1000, 0, cached_tokens=99999) == 100


def test_billed_of_event_fallback():
    import json as _j

    from database.models import LLMUsageEvent
    from llm.usage_tracker import _billed_of

    with_billed = LLMUsageEvent(
        total_tokens=212_000, usage_metadata=_j.dumps({"billed_tokens": 46_000})
    )
    assert _billed_of(with_billed) == 46_000
    # Pre-existing rows without billed accounting fall back to the raw total.
    legacy = LLMUsageEvent(total_tokens=212_000, usage_metadata=_j.dumps({"latency_ms": 5}))
    assert _billed_of(legacy) == 212_000
    no_meta = LLMUsageEvent(total_tokens=500, usage_metadata=None)
    assert _billed_of(no_meta) == 500

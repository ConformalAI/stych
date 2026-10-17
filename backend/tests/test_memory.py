"""
Tests for agents/memory — ShortTermMemory recent tail, token counting,
context window registry, and MemoryManager.

Uses direct importlib loading (same pattern as test_react_engine.py) so the
heavy agents/__init__.py chain (which needs a live DB) is never triggered.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Direct module loading — bypass agents/__init__.py
# ---------------------------------------------------------------------------


def _load_memory_modules():
    root = Path(__file__).resolve().parents[1]
    mem_dir = root / "memory"

    # Register stub package so intra-package relative imports resolve
    mem_pkg = types.ModuleType("memory")
    mem_pkg.__path__ = [str(mem_dir)]  # type: ignore[attr-defined]
    sys.modules.setdefault("memory", mem_pkg)

    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        assert spec and spec.loader, f"Could not load {path}"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    _load("memory.interface", mem_dir / "interface.py")
    token_mod = _load("memory.token_counter", mem_dir / "token_counter.py")
    ctx_mod = _load("memory.context_window", mem_dir / "context_window.py")
    st_mod = _load("memory.short_term", mem_dir / "short_term.py")
    _load("memory.compaction", mem_dir / "compaction.py")
    mgr_mod = _load("memory.manager", mem_dir / "manager.py")
    return token_mod, ctx_mod, st_mod, mgr_mod


_token_mod, _ctx_mod, _st_mod, _mgr_mod = _load_memory_modules()

count = _token_mod.count
count_messages = _token_mod.count_messages
estimate_overhead = _token_mod.estimate_overhead

get_context_window = _ctx_mod.get_context_window
get_token_budget = _ctx_mod.get_token_budget

ShortTermMemory = _st_mod.ShortTermMemory
MemoryManager = _mgr_mod.MemoryManager


# ---------------------------------------------------------------------------
# Minimal Message/AgentState stubs (no DB dependency)
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    role: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _AgentState:
    session_id: str
    messages: List[_Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# token_counter
# ===========================================================================


class TestTokenCounter:
    def test_empty_string(self):
        assert count("") == 0

    def test_nonzero_for_text(self):
        assert count("hello world") > 0

    def test_count_messages_empty(self):
        assert count_messages([]) == 0

    def test_count_messages_adds_overhead(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert count_messages(msgs) >= count("hello")

    def test_estimate_overhead_nonzero(self):
        oh = estimate_overhead("You are an assistant.", "Dataset: 100 rows")
        assert oh > 0


# ===========================================================================
# context_window
# ===========================================================================


class TestContextWindow:
    def test_known_claude_model(self):
        assert get_context_window("claude-sonnet-4") == 200_000

    def test_known_gemini_model(self):
        assert get_context_window("gemini-2.5-pro") == 1_000_000

    def test_openrouter_prefixed_model(self):
        assert get_context_window("openrouter/anthropic/claude-sonnet-4.5") == 200_000

    def test_unknown_model_returns_default(self):
        assert get_context_window("some-unknown-model-xyz") == 128_000

    def test_none_returns_default(self):
        assert get_context_window(None) == 128_000

    def test_token_budget_70_percent(self):
        assert get_token_budget("claude-sonnet-4", fraction=0.70) == int(200_000 * 0.70)

    def test_token_budget_fraction_respected(self):
        b1 = get_token_budget("claude-sonnet-4", fraction=0.50)
        b2 = get_token_budget("claude-sonnet-4", fraction=0.90)
        assert b1 < b2


# ===========================================================================
# ShortTermMemory
# ===========================================================================


def _msgs(n: int, role: str = "user", chars_each: int = 50) -> List[_Message]:
    return [_Message(role=role, content=f"Message {i}: " + "x" * chars_each) for i in range(n)]


class TestShortTermMemory:
    def test_empty_messages_returns_empty(self):
        assert ShortTermMemory().get_window([], model="claude-sonnet-4") == []

    def test_returns_list_of_dicts(self):
        result = ShortTermMemory().get_window(_msgs(3), model="claude-sonnet-4")
        assert all("role" in m and "content" in m for m in result)

    def test_rolling_window_respects_budget(self):
        msgs = _msgs(200, chars_each=200)
        # budget_fraction=0.01 of 128k default ≈ 1280 tokens → much less than 200 messages
        result = ShortTermMemory(budget_fraction=0.01).get_window(msgs, model="default")
        assert len(result) < 200

    def test_floor_guarantees_at_least_one_message(self):
        msgs = _msgs(5, chars_each=10)
        result = ShortTermMemory(budget_fraction=0.0).get_window(msgs, model="claude-sonnet-4")
        assert len(result) >= 1

    def test_chronological_order_preserved(self):
        msgs = [_Message(role="user", content=f"Turn {i}") for i in range(10)]
        result = ShortTermMemory().get_window(msgs, model="claude-sonnet-4")
        indices = [int(r["content"].split()[1]) for r in result]
        assert indices == sorted(indices)

    def test_overhead_tokens_reduce_window(self):
        msgs = _msgs(50, chars_each=100)
        stm = ShortTermMemory(budget_fraction=0.10)
        no_oh = stm.get_window(msgs, overhead_tokens=0, model="default")
        with_oh = stm.get_window(msgs, overhead_tokens=5000, model="default")
        assert len(with_oh) <= len(no_oh)

    def test_summarized_count_skips_older_messages(self):
        msgs = [_Message(role="user", content=f"Turn {i}") for i in range(10)]
        # Watermark of 7 → only the last 3 messages form the verbatim tail.
        result = ShortTermMemory().get_window(msgs, summarized_count=7, model="claude-sonnet-4")
        indices = [int(r["content"].split()[1]) for r in result]
        assert indices == [7, 8, 9]

    def test_unknown_model_uses_default_window(self):
        result = ShortTermMemory().get_window(_msgs(5), model="totally-unknown-model-xyz")
        assert isinstance(result, list)

    def test_newest_messages_preferred(self):
        msgs = [_Message(role="user", content=f"Turn {i}") for i in range(20)]
        result = ShortTermMemory(budget_fraction=0.005).get_window(msgs, model="claude-sonnet-4")
        assert result, "Should contain at least 1 message (floor)"
        assert "Turn 19" in result[-1]["content"]


# ===========================================================================
# MemoryManager
# ===========================================================================


class TestMemoryManager:
    def test_get_context_window_delegates(self):
        result = MemoryManager(short_term=ShortTermMemory()).get_context_window(
            _msgs(5), model="claude-sonnet-4"
        )
        assert isinstance(result, list)

    def test_no_long_term_by_default(self):
        assert MemoryManager(short_term=ShortTermMemory()).long_term is None

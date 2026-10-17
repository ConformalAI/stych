"""
Tests for cross-turn conversation compaction (memory/compaction.py + MemoryManager).

Drives compaction with a deliberately low threshold and a scripted fake LLM
router — no network. Modules are loaded in isolation (same pattern as
tests/test_memory.py) so the heavy agents/__init__ chain is never triggered.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


def _load_memory_modules():
    root = Path(__file__).resolve().parents[1]
    mem_dir = root / "memory"

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
    _load("memory.token_counter", mem_dir / "token_counter.py")
    _load("memory.context_window", mem_dir / "context_window.py")
    _load("memory.short_term", mem_dir / "short_term.py")
    comp_mod = _load("memory.compaction", mem_dir / "compaction.py")
    mgr_mod = _load("memory.manager", mem_dir / "manager.py")
    return comp_mod, mgr_mod


_comp_mod, _mgr_mod = _load_memory_modules()

should_compact = _comp_mod.should_compact
compact_conversation = _comp_mod.compact_conversation
SUMMARY_META_KEY = _comp_mod.SUMMARY_META_KEY
SUMMARIZED_COUNT_META_KEY = _comp_mod.SUMMARIZED_COUNT_META_KEY
MemoryManager = _mgr_mod.MemoryManager
ShortTermMemory = sys.modules["memory.short_term"].ShortTermMemory


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    role: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _AgentState:
    session_id: str = "s1"
    messages: List[_Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _ScriptedRouter:
    """Records requests and returns a canned summary from generate()."""

    def __init__(self, summary: str = "Summary: earlier turns condensed."):
        self.summary = summary
        self.requests: List[Any] = []

    async def generate(self, request):
        self.requests.append(request)
        return _FakeResponse(self.summary)

    def last_user_content(self) -> str:
        req = self.requests[-1]
        return req.messages[-1].content


def _msgs(n: int) -> List[_Message]:
    role = ["user", "assistant"]
    return [_Message(role=role[i % 2], content=f"Turn {i}: " + "word " * 20) for i in range(n)]


_MODEL = "claude-sonnet-4"
_LOW_CFG = {
    "enabled": True,
    "trigger_fraction": 0.0001,  # ~20 tokens on a 200k window → always fires
    "keep_recent_messages": 3,
    "max_summary_tokens": 500,
}


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    def test_empty_is_false(self):
        assert should_compact([], _MODEL, 0.6) is False

    def test_low_threshold_fires(self):
        assert should_compact(_msgs(5), _MODEL, 0.0001) is True

    def test_high_threshold_does_not_fire(self):
        assert should_compact(_msgs(5), _MODEL, 0.9) is False


# ---------------------------------------------------------------------------
# maybe_compact_conversation (integration)
# ---------------------------------------------------------------------------


class TestMaybeCompact:
    async def test_low_threshold_compacts(self):
        state = _AgentState()
        messages = _msgs(10)
        router = _ScriptedRouter()
        mgr = MemoryManager(short_term=ShortTermMemory())

        await mgr.maybe_compact_conversation(
            state, messages, llm_router=router, model=_MODEL, cfg=_LOW_CFG
        )

        # Summary cached + watermark advanced to len - keep_recent.
        assert state.metadata[SUMMARY_META_KEY] == router.summary
        assert state.metadata[SUMMARIZED_COUNT_META_KEY] == 10 - 3

        # The router summarized the older messages, not the recent tail.
        assert len(router.requests) == 1
        seen = router.last_user_content()
        assert "Turn 0" in seen and "Turn 6" in seen
        assert "Turn 9" not in seen  # last 3 kept verbatim

    async def test_idempotent_no_new_messages(self):
        state = _AgentState()
        messages = _msgs(10)
        router = _ScriptedRouter()
        mgr = MemoryManager(short_term=ShortTermMemory())

        await mgr.maybe_compact_conversation(
            state, messages, llm_router=router, model=_MODEL, cfg=_LOW_CFG
        )
        # Second call with the same messages must not re-summarize.
        await mgr.maybe_compact_conversation(
            state, messages, llm_router=router, model=_MODEL, cfg=_LOW_CFG
        )
        assert len(router.requests) == 1

    async def test_high_threshold_no_llm_call(self):
        state = _AgentState()
        messages = _msgs(10)
        router = _ScriptedRouter()
        mgr = MemoryManager(short_term=ShortTermMemory())

        await mgr.maybe_compact_conversation(
            state,
            messages,
            llm_router=router,
            model=_MODEL,
            cfg={**_LOW_CFG, "trigger_fraction": 0.9},
        )
        assert router.requests == []
        assert SUMMARY_META_KEY not in state.metadata

    async def test_disabled_no_op(self):
        state = _AgentState()
        router = _ScriptedRouter()
        mgr = MemoryManager(short_term=ShortTermMemory())
        await mgr.maybe_compact_conversation(
            state, _msgs(10), llm_router=router, model=_MODEL, cfg={**_LOW_CFG, "enabled": False}
        )
        assert router.requests == []

    async def test_window_after_compaction_is_recent_tail(self):
        state = _AgentState()
        messages = _msgs(10)
        router = _ScriptedRouter()
        mgr = MemoryManager(short_term=ShortTermMemory())

        await mgr.maybe_compact_conversation(
            state, messages, llm_router=router, model=_MODEL, cfg=_LOW_CFG
        )
        watermark = state.metadata[SUMMARIZED_COUNT_META_KEY]
        window = mgr.get_context_window(messages, summarized_count=watermark, model=_MODEL)
        # Only the recent tail remains verbatim; older turns live in the summary.
        assert len(window) == 3
        assert window[0]["content"].startswith("Turn 7")

    async def test_llm_error_keeps_prior_summary(self):
        class _BoomRouter:
            async def generate(self, request):
                raise RuntimeError("boom")

        state = _AgentState()
        messages = _msgs(10)
        mgr = MemoryManager(short_term=ShortTermMemory())
        await mgr.maybe_compact_conversation(
            state, messages, llm_router=_BoomRouter(), model=_MODEL, cfg=_LOW_CFG
        )
        # Failure must not set a summary or advance the watermark.
        assert SUMMARY_META_KEY not in state.metadata
        assert SUMMARIZED_COUNT_META_KEY not in state.metadata

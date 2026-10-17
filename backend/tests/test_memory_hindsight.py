"""
Tests for agents/memory/hindsight — HindsightLongTermMemory and the new
MemoryManager methods (recall_long_term, retain_turn).

Uses the same importlib.util direct-loading pattern as test_memory.py so
agents/__init__.py (which needs a live DB) is never triggered.

All Hindsight HTTP calls are mocked with unittest.mock.AsyncMock — no live
server is required.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Direct module loading — bypass agents/__init__.py
# ---------------------------------------------------------------------------


def _load_memory_modules():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    mem_dir = root / "memory"

    mem_pkg = types.ModuleType("memory")
    mem_pkg.__path__ = [str(mem_dir)]  # type: ignore[attr-defined]
    sys.modules.setdefault("memory", mem_pkg)

    def _load(name: str, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    _load("memory.interface", mem_dir / "interface.py")
    _load("memory.token_counter", mem_dir / "token_counter.py")
    _load("memory.context_window", mem_dir / "context_window.py")
    st_mod = _load("memory.short_term", mem_dir / "short_term.py")
    hindsight_mod = _load("memory.hindsight", mem_dir / "hindsight.py")
    mgr_mod = _load("memory.manager", mem_dir / "manager.py")
    return hindsight_mod, mgr_mod, st_mod


_hindsight_mod, _mgr_mod, _st_mod = _load_memory_modules()

HindsightLongTermMemory = _hindsight_mod.HindsightLongTermMemory
MemoryManager = _mgr_mod.MemoryManager
ShortTermMemory = _st_mod.ShortTermMemory


# ---------------------------------------------------------------------------
# Minimal stubs
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


def _make_fact(text: str) -> MagicMock:
    """Return a mock Hindsight result fact with a .text attribute."""
    f = MagicMock()
    f.text = text
    return f


# ===========================================================================
# HindsightLongTermMemory — bank ID formation
# ===========================================================================


class TestBankId:
    def test_with_user_id(self):
        h = HindsightLongTermMemory(bank_id_prefix="stych")
        assert h._bank_id("alice") == "stych:user:alice"

    def test_without_user_id_falls_back(self):
        h = HindsightLongTermMemory(bank_id_prefix="stych")
        assert h._bank_id(None) == "stych:default"

    def test_empty_user_id_falls_back(self):
        h = HindsightLongTermMemory(bank_id_prefix="stych")
        assert h._bank_id("") == "stych:default"

    def test_custom_prefix(self):
        h = HindsightLongTermMemory(bank_id_prefix="myapp")
        assert h._bank_id("u1") == "myapp:user:u1"


# ===========================================================================
# HindsightLongTermMemory.recall()
# ===========================================================================


class TestHindsightRecall:
    def _make_client_with_results(self, facts: List[str]) -> MagicMock:
        response = MagicMock()
        response.results = [_make_fact(f) for f in facts]
        client = MagicMock()
        client.arecall = AsyncMock(return_value=response)
        return client

    def _make_empty_client(self) -> MagicMock:
        response = MagicMock()
        response.results = []
        client = MagicMock()
        client.arecall = AsyncMock(return_value=response)
        return client

    @pytest.mark.asyncio
    async def test_returns_bullet_list(self):
        h = HindsightLongTermMemory()
        h._client = self._make_client_with_results(["Fact A", "Fact B"])
        result = await h.recall("test query", user_id="u1")
        assert "- Fact A" in result
        assert "- Fact B" in result

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_string(self):
        h = HindsightLongTermMemory()
        h._client = self._make_empty_client()
        result = await h.recall("test query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_graceful_fallback_on_error(self):
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        h._client.arecall = AsyncMock(side_effect=ConnectionError("server down"))
        result = await h.recall("test query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_passes_correct_bank_id(self):
        h = HindsightLongTermMemory(bank_id_prefix="stych")
        h._client = self._make_empty_client()
        await h.recall("q", user_id="bob")
        call_kwargs = h._client.arecall.call_args
        assert call_kwargs.kwargs["bank_id"] == "stych:user:bob"

    @pytest.mark.asyncio
    async def test_passes_budget_and_max_tokens(self):
        h = HindsightLongTermMemory(recall_budget="low", recall_max_tokens=512)
        h._client = self._make_empty_client()
        await h.recall("q")
        call_kwargs = h._client.arecall.call_args
        assert call_kwargs.kwargs["budget"] == "low"
        assert call_kwargs.kwargs["max_tokens"] == 512

    @pytest.mark.asyncio
    async def test_max_tokens_override(self):
        h = HindsightLongTermMemory(recall_max_tokens=512)
        h._client = self._make_empty_client()
        await h.recall("q", max_tokens=256)
        call_kwargs = h._client.arecall.call_args
        assert call_kwargs.kwargs["max_tokens"] == 256

    @pytest.mark.asyncio
    async def test_includes_world_observation_and_experience(self):
        # "experience" facts are created immediately on retain (before Hindsight's
        # async extraction pipeline promotes them to "world"/"observation").
        # All three types must be requested so freshly retained content is visible.
        h = HindsightLongTermMemory()
        h._client = self._make_empty_client()
        await h.recall("q")
        call_kwargs = h._client.arecall.call_args
        types = call_kwargs.kwargs["types"]
        assert "world" in types
        assert "observation" in types
        assert "experience" in types


# ===========================================================================
# HindsightLongTermMemory.retain()
# ===========================================================================


class TestHindsightRetain:
    def _make_retain_client(self) -> MagicMock:
        client = MagicMock()
        client.aretain = AsyncMock(return_value=None)
        return client

    @pytest.mark.asyncio
    async def test_does_not_raise_on_success(self):
        h = HindsightLongTermMemory()
        h._client = self._make_retain_client()
        await h.retain("content", session_id="s1", user_id="u1")

    @pytest.mark.asyncio
    async def test_does_not_raise_on_connection_error(self):
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        h._client.aretain = AsyncMock(side_effect=ConnectionError("offline"))
        # Must not propagate
        await h.retain("content", session_id="s1")

    @pytest.mark.asyncio
    async def test_session_tag_added(self):
        h = HindsightLongTermMemory()
        h._client = self._make_retain_client()
        await h.retain("content", session_id="sess-42")
        tags = h._client.aretain.call_args.kwargs["tags"]
        assert "session:sess-42" in tags

    @pytest.mark.asyncio
    async def test_extra_tags_forwarded(self):
        h = HindsightLongTermMemory()
        h._client = self._make_retain_client()
        await h.retain("content", session_id="s1", tags=["dataset:sales.csv"])
        tags = h._client.aretain.call_args.kwargs["tags"]
        assert "dataset:sales.csv" in tags
        assert "session:s1" in tags

    @pytest.mark.asyncio
    async def test_document_id_forwarded(self):
        h = HindsightLongTermMemory()
        h._client = self._make_retain_client()
        await h.retain("content", session_id="s1", document_id="s1:turn:3")
        doc_id = h._client.aretain.call_args.kwargs["document_id"]
        assert doc_id == "s1:turn:3"

    @pytest.mark.asyncio
    async def test_context_label_set(self):
        h = HindsightLongTermMemory()
        h._client = self._make_retain_client()
        await h.retain("content", session_id="s1")
        ctx = h._client.aretain.call_args.kwargs["context"]
        assert "data analysis" in ctx.lower()

    @pytest.mark.asyncio
    async def test_extraction_mode_pinned_once_per_bank(self):
        """First retain pins retain_extraction_mode=concise; later retains skip it."""
        h = HindsightLongTermMemory(retain_extraction_mode="concise")
        h._client = self._make_retain_client()
        h._client.banks = MagicMock()
        h._client.banks.update_bank_config = AsyncMock(return_value=None)

        await h.retain("first", session_id="s1", user_id="u1")
        await h.retain("second", session_id="s1", user_id="u1")

        # Config PATCH fires exactly once for the bank, with concise mode.
        h._client.banks.update_bank_config.assert_called_once()
        bank_arg, cfg_update = h._client.banks.update_bank_config.call_args.args
        assert bank_arg == "stych:user:u1"
        assert cfg_update.updates["retain_extraction_mode"] == "concise"

    @pytest.mark.asyncio
    async def test_retain_survives_config_patch_failure(self):
        """A failing bank-config PATCH must not stop the turn from being retained."""
        h = HindsightLongTermMemory()
        h._client = self._make_retain_client()
        h._client.banks = MagicMock()
        h._client.banks.update_bank_config = AsyncMock(side_effect=ConnectionError("no config api"))

        await h.retain("content", session_id="s1", user_id="u1")
        h._client.aretain.assert_called_once()


# ===========================================================================
# MemoryManager — new async methods
# ===========================================================================


class TestMemoryManagerLongTerm:
    def _manager(self, hindsight=None) -> MemoryManager:
        return MemoryManager(short_term=ShortTermMemory(), hindsight=hindsight)

    # recall_long_term
    @pytest.mark.asyncio
    async def test_recall_long_term_returns_empty_when_no_hindsight(self):
        mgr = self._manager(hindsight=None)
        result = await mgr.recall_long_term("query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_recall_long_term_delegates_to_hindsight(self):
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        resp = MagicMock()
        resp.results = [_make_fact("remembered fact")]
        h._client.arecall = AsyncMock(return_value=resp)

        mgr = self._manager(hindsight=h)
        result = await mgr.recall_long_term("what did we do?", user_id="u1")
        assert "remembered fact" in result

    @pytest.mark.asyncio
    async def test_recall_long_term_passes_user_id(self):
        h = HindsightLongTermMemory(bank_id_prefix="stych")
        h._client = MagicMock()
        resp = MagicMock()
        resp.results = []
        h._client.arecall = AsyncMock(return_value=resp)

        mgr = self._manager(hindsight=h)
        await mgr.recall_long_term("q", user_id="carol")
        bank = h._client.arecall.call_args.kwargs["bank_id"]
        assert bank == "stych:user:carol"

    # retain_turn
    @pytest.mark.asyncio
    async def test_retain_turn_noop_when_no_hindsight(self):
        mgr = self._manager(hindsight=None)
        # Must not raise
        await mgr.retain_turn("hi", "hello", session_id="s1")

    @pytest.mark.asyncio
    async def test_retain_turn_formats_content(self):
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        h._client.aretain = AsyncMock(return_value=None)

        mgr = self._manager(hindsight=h)
        await mgr.retain_turn("user msg", "assistant msg", session_id="s1", user_id="u1")
        content = h._client.aretain.call_args.kwargs["content"]
        assert "User: user msg" in content
        assert "Assistant: assistant msg" in content

    @pytest.mark.asyncio
    async def test_retain_turn_document_id_uses_turn_index(self):
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        h._client.aretain = AsyncMock(return_value=None)

        mgr = self._manager(hindsight=h)
        await mgr.retain_turn("u", "a", session_id="sess-7", user_id="u1", turn_index=4)
        doc_id = h._client.aretain.call_args.kwargs["document_id"]
        assert doc_id == "sess-7:turn:4"

    @pytest.mark.asyncio
    async def test_retain_turn_dataset_tag(self):
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        h._client.aretain = AsyncMock(return_value=None)

        mgr = self._manager(hindsight=h)
        await mgr.retain_turn("u", "a", session_id="s1", user_id="u1", dataset_filename="sales.csv")
        tags = h._client.aretain.call_args.kwargs["tags"]
        assert "dataset:sales.csv" in tags

    @pytest.mark.asyncio
    async def test_retain_turn_skipped_when_no_user_id(self):
        """retain_turn must not write to the default bank when user_id is absent."""
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        h._client.aretain = AsyncMock(return_value=None)

        mgr = self._manager(hindsight=h)
        await mgr.retain_turn("u", "a", session_id="s1", user_id=None)
        h._client.aretain.assert_not_called()

    @pytest.mark.asyncio
    async def test_recall_long_term_skipped_when_no_user_id(self):
        """recall_long_term must not query the default bank when user_id is absent."""
        h = HindsightLongTermMemory()
        h._client = MagicMock()
        resp = MagicMock()
        resp.results = [_make_fact("some fact")]
        h._client.arecall = AsyncMock(return_value=resp)

        mgr = self._manager(hindsight=h)
        result = await mgr.recall_long_term("query", user_id=None)
        assert result == ""
        h._client.arecall.assert_not_called()

    # Constructor
    def test_hindsight_is_none_by_default(self):
        mgr = MemoryManager(short_term=ShortTermMemory())
        assert mgr.hindsight is None

    def test_hindsight_stored_when_provided(self):
        h = HindsightLongTermMemory()
        mgr = MemoryManager(short_term=ShortTermMemory(), hindsight=h)
        assert mgr.hindsight is h

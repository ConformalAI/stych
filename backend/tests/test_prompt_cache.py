"""
Tests for Anthropic prompt-cache breakpoint placement (llm/prompt_cache.py) and
that the LiteLLM provider applies it before hitting the API.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.prompt_cache import apply_cache_control, model_supports_caching


def _msgs():
    return [
        {"role": "system", "content": "you are an agent\n<tools...>"},
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "observation text"},
    ]


def test_no_op_for_non_anthropic():
    msgs = _msgs()
    out = apply_cache_control(msgs, "gpt-4o")
    assert out is msgs  # unchanged, same object
    assert all(isinstance(m["content"], (str, type(None))) for m in out)


def test_breakpoints_on_system_and_last_message_for_claude():
    out = apply_cache_control(_msgs(), "anthropic/claude-sonnet-4.5")

    # System (static prefix: tools + system) carries a breakpoint.
    sys_content = out[0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[0]["cache_control"] == {"type": "ephemeral"}

    # Last message (rolling conversation prefix) carries a breakpoint.
    last_content = out[-1]["content"]
    assert isinstance(last_content, list)
    assert last_content[-1]["cache_control"] == {"type": "ephemeral"}

    # Middle messages are untouched (no extra breakpoints — within Anthropic's 4).
    assert out[1]["content"] == "do the thing"
    breakpointed = sum(
        1
        for m in out
        if isinstance(m["content"], list) and any(p.get("cache_control") for p in m["content"])
    )
    assert breakpointed == 2


def test_assistant_tool_call_message_not_chosen_as_breakpoint():
    # If the last message has no text content (assistant tool_calls only), it is
    # left alone — only the system breakpoint applies.
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
    ]
    out = apply_cache_control(msgs, "claude-sonnet-4")
    assert isinstance(out[0]["content"], list)  # system breakpoint applied
    assert out[1]["content"] is None  # unchanged — can't host a breakpoint


def test_model_support_predicate():
    assert model_supports_caching("openrouter/anthropic/claude-sonnet-4.5")
    assert model_supports_caching("claude-3-5-sonnet")
    assert not model_supports_caching("gpt-4o")
    assert not model_supports_caching("gemini-2.5-pro")
    assert not model_supports_caching(None)


@pytest.mark.asyncio
async def test_litellm_provider_sends_cache_control_for_claude():
    from llm.litellm_provider import LiteLLMProvider
    from llm.models import LLMRequest, Message

    provider = LiteLLMProvider(base_url="http://x", api_key="k", default_model="claude-sonnet-4.5")

    captured = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        resp = MagicMock()
        choice = MagicMock()
        choice.message.content = "ok"
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        resp.choices = [choice]
        resp.model = "claude-sonnet-4.5"
        resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return resp

    provider.client.chat.completions.create = AsyncMock(side_effect=_create)
    await provider.generate(
        LLMRequest(
            messages=[Message(role="system", content="sys"), Message(role="user", content="hi")]
        )
    )
    sent = captured["messages"]
    # The system message went out with a cache_control breakpoint.
    assert isinstance(sent[0]["content"], list)
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

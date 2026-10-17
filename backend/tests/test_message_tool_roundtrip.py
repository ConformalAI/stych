"""
Phase 0 foundation tests: native tool-call round-trip through the Message model
and the OpenAI-compatible providers.

The Message model is loaded in isolation (it only needs pydantic) so this test
does not pull in the heavy llm/__init__ (google.genai / openai) just to check
serialization. The provider id-preservation check mocks the OpenAI client.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_models_module():
    """Load llm.models without triggering llm/__init__ (avoids openai/genai)."""
    root = Path(__file__).resolve().parents[1]
    llm_pkg = types.ModuleType("llm")
    llm_pkg.__path__ = [str(root / "llm")]  # type: ignore[attr-defined]
    sys.modules.setdefault("llm", llm_pkg)

    models_path = root / "llm" / "models.py"
    spec = importlib.util.spec_from_file_location("llm.models", str(models_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llm.models"] = mod
    spec.loader.exec_module(mod)  # type: ignore[assignment]
    return mod


models = _load_models_module()


def test_assistant_tool_call_message_serializes_to_openai_shape():
    msg = models.Message(
        role="assistant",
        content=None,
        tool_calls=[
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "run_python", "arguments": '{"code": "1+1"}'},
            }
        ],
    )
    dumped = msg.model_dump(exclude_none=True)
    # content is None → excluded; tool_calls preserved; role intact.
    assert dumped == {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "run_python", "arguments": '{"code": "1+1"}'},
            }
        ],
    }


def test_tool_result_message_serializes_with_call_id():
    msg = models.Message(role="tool", content="result: 2", tool_call_id="call_abc")
    dumped = msg.model_dump(exclude_none=True)
    assert dumped == {"role": "tool", "content": "result: 2", "tool_call_id": "call_abc"}


def test_plain_text_message_unchanged():
    """A plain message must serialize exactly as before (no new keys leak)."""
    msg = models.Message(role="user", content="hello")
    assert msg.model_dump(exclude_none=True) == {"role": "user", "content": "hello"}


@pytest.mark.asyncio
async def test_litellm_generate_preserves_tool_call_id():
    """generate() must surface the provider tool-call id in response metadata."""
    from llm.litellm_provider import LiteLLMProvider
    from llm.models import LLMRequest, Message

    provider = LiteLLMProvider(base_url="http://x", api_key="k", default_model="m")

    fake_tc = MagicMock()
    fake_tc.id = "call_xyz"
    fake_tc.function.name = "run_python"
    fake_tc.function.arguments = '{"code": "df.head()"}'

    fake_choice = MagicMock()
    fake_choice.message.content = ""
    fake_choice.message.tool_calls = [fake_tc]
    fake_choice.finish_reason = "tool_calls"

    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.model = "m"
    fake_resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    provider.client.chat.completions.create = AsyncMock(return_value=fake_resp)

    resp = await provider.generate(
        LLMRequest(messages=[Message(role="user", content="hi")], tools=[{"x": 1}])
    )
    tcs = resp.metadata["tool_calls"]
    assert tcs[0]["id"] == "call_xyz"
    assert tcs[0]["name"] == "run_python"
    assert tcs[0]["arguments"] == {"code": "df.head()"}
    assert json.dumps(tcs[0]["arguments"])  # arguments is a real dict

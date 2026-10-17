"""
LiteLLM provider integration for Stych.
Supports multiple LLM providers through a unified interface.
"""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from .models import LLMRequest, LLMResponse, LLMStreamChunk, usage_to_dict
from .prompt_cache import apply_cache_control

logger = logging.getLogger(__name__)


def _safe_tool_call_args(raw: Optional[str], tool_name: str) -> dict:
    """
    Parse a tool-call's ``arguments`` JSON defensively. A malformed payload (e.g. the
    model emitting invalid JSON for a large multi-line SQL argument) must not raise and
    kill the whole request — coerce to ``{}`` so the agent loop can surface a clear,
    recoverable tool error instead.
    """
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        logger.warning("Malformed tool-call arguments for tool '%s'; coercing to {}", tool_name)
        return {}


def _normalize_messages(messages: list) -> list:
    """
    Normalise the message list so it is accepted by strict local models (e.g.
    vLLM-hosted Qwen3) as well as every OpenAI-compatible cloud endpoint.

    Rules applied:
    1. Collect all system messages and merge their content into a single block.
    2. Place that single system message first.
    3. Keep the remaining user/assistant turns in their original order.

    Merging (rather than just reordering) is necessary because Qwen3's chat
    template raises "System message must be at the beginning" when it sees
    *any* system message after position 0 — including a second system message
    that is itself at the top of the list.
    """
    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    if not system_parts:
        return rest

    merged_system = {"role": "system", "content": "\n\n".join(system_parts)}
    return [merged_system] + rest


class LiteLLMProvider:
    """
    LiteLLM provider that supports multiple LLM backends.
    Can connect to local LiteLLM server or use direct model access.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:4000",
        api_key: Optional[str] = None,
        default_model: str = "gpt-4",
        fallback_models: Optional[List[str]] = None,
    ):
        self.base_url = base_url
        self.api_key = api_key or "dummy-key"  # LiteLLM may not need API key
        self.default_model = default_model
        self.fallback_models = fallback_models or []

        # Initialize OpenAI-compatible client pointing to LiteLLM
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=base_url,
        )

        logger.info(f"LiteLLM provider initialized with base_url: {base_url}")

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate response using LiteLLM."""
        model = request.model or self.default_model
        messages = _normalize_messages(
            [msg.model_dump(exclude_none=True) for msg in request.messages]
        )
        # Prompt-cache breakpoints on the static + rolling prefix (Anthropic only).
        messages = apply_cache_control(messages, model)

        try:
            create_kwargs: Dict[str, Any] = dict(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stream=False,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
            )
            if request.tools:
                create_kwargs["tools"] = request.tools
                if request.tool_choice is not None:
                    create_kwargs["tool_choice"] = request.tool_choice

            response = await self.client.chat.completions.create(**create_kwargs)

            choice = response.choices[0]
            tool_calls_raw = getattr(choice.message, "tool_calls", None) or []
            tool_calls = [
                {
                    # Preserve the provider call id so the agent loop can build a
                    # spec-correct assistant message and correlate the tool result.
                    "id": getattr(tc, "id", None),
                    "name": tc.function.name,
                    "arguments": _safe_tool_call_args(tc.function.arguments, tc.function.name),
                }
                for tc in tool_calls_raw
            ]
            return LLMResponse(
                content=choice.message.content or "",
                model=response.model,
                usage=usage_to_dict(response.usage),
                finish_reason=choice.finish_reason,
                metadata={
                    "provider": "litellm",
                    "base_url": self.base_url,
                    "tool_calls": tool_calls if tool_calls else None,
                },
            )

        except Exception as e:
            logger.error(f"LiteLLM API error with model {model}: {e}")
            raise Exception(f"LiteLLM error for model '{model}': {e}")

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Generate streaming response using LiteLLM."""
        import json as _json

        model = request.model or self.default_model
        messages = _normalize_messages(
            [msg.model_dump(exclude_none=True) for msg in request.messages]
        )
        # Prompt-cache breakpoints on the static + rolling prefix (Anthropic only).
        messages = apply_cache_control(messages, model)

        try:
            create_kwargs: dict = dict(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stream=True,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
            )
            if request.tools:
                create_kwargs["tools"] = request.tools
                if request.tool_choice is not None:
                    create_kwargs["tool_choice"] = request.tool_choice

            # Ask for real token usage on the final stream chunk. Some backends
            # reject stream_options — retry once without it rather than failing.
            try:
                stream = await self.client.chat.completions.create(
                    **create_kwargs, stream_options={"include_usage": True}
                )
            except Exception as opt_err:
                logger.warning(
                    "stream_options rejected by backend (%s); retrying without usage", opt_err
                )
                stream = await self.client.chat.completions.create(**create_kwargs)

            usage_final: Optional[dict] = None
            tc_names: dict = {}
            tc_args: dict = {}
            tc_ids: dict = {}
            has_tool_calls = False
            # State machine for inline <think>...</think> blocks.
            # Ollama-hosted Qwen3 (and some other local models) embed their
            # chain-of-thought directly inside delta.content using these tags
            # rather than using the reasoning_content field.  We parse them out
            # here so every caller receives properly-typed is_thinking chunks.
            _in_inline_think = False

            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_final = usage_to_dict(chunk.usage)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                # Qwen3 / DeepSeek-R1 and other reasoning models expose internal
                # chain-of-thought via a separate `reasoning_content` field on the
                # delta.  Yield it as a dedicated chunk so the caller can route it
                # to a separate thinking section in the UI.
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    yield LLMStreamChunk(
                        content=rc,
                        finish_reason=finish_reason,
                        metadata={
                            "provider": "litellm",
                            "base_url": self.base_url,
                            "model": model,
                            "is_tool_call": False,
                            "is_thinking": True,
                        },
                    )

                if delta.content:
                    # Split content around <think>...</think> blocks so that
                    # thinking text is always yielded with is_thinking=True,
                    # regardless of whether the model uses reasoning_content or
                    # inline tags.  A while loop handles the case where a single
                    # delta chunk contains multiple tag transitions.
                    remaining = delta.content
                    while remaining:
                        if not _in_inline_think:
                            think_start = remaining.find("<think>")
                            if think_start == -1:
                                yield LLMStreamChunk(
                                    content=remaining,
                                    finish_reason=finish_reason,
                                    metadata={
                                        "provider": "litellm",
                                        "base_url": self.base_url,
                                        "model": model,
                                        "is_tool_call": False,
                                        "is_thinking": False,
                                    },
                                )
                                remaining = ""
                            else:
                                # Emit any text that precedes the opening tag.
                                if think_start > 0:
                                    yield LLMStreamChunk(
                                        content=remaining[:think_start],
                                        finish_reason=finish_reason,
                                        metadata={
                                            "provider": "litellm",
                                            "base_url": self.base_url,
                                            "model": model,
                                            "is_tool_call": False,
                                            "is_thinking": False,
                                        },
                                    )
                                _in_inline_think = True
                                remaining = remaining[think_start + len("<think>") :]
                        else:
                            think_end = remaining.find("</think>")
                            if think_end == -1:
                                # Entire remainder is thinking content.
                                yield LLMStreamChunk(
                                    content=remaining,
                                    finish_reason=finish_reason,
                                    metadata={
                                        "provider": "litellm",
                                        "base_url": self.base_url,
                                        "model": model,
                                        "is_tool_call": False,
                                        "is_thinking": True,
                                    },
                                )
                                remaining = ""
                            else:
                                # Emit the thinking body before the close tag.
                                if think_end > 0:
                                    yield LLMStreamChunk(
                                        content=remaining[:think_end],
                                        finish_reason=finish_reason,
                                        metadata={
                                            "provider": "litellm",
                                            "base_url": self.base_url,
                                            "model": model,
                                            "is_tool_call": False,
                                            "is_thinking": True,
                                        },
                                    )
                                _in_inline_think = False
                                remaining = remaining[think_end + len("</think>") :]

                tc_list = getattr(delta, "tool_calls", None)
                if tc_list:
                    has_tool_calls = True
                    for tc in tc_list:
                        idx = getattr(tc, "index", 0)
                        if getattr(tc, "id", None):
                            tc_ids[idx] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn:
                            if getattr(fn, "name", None):
                                tc_names[idx] = fn.name
                            if getattr(fn, "arguments", None):
                                tc_args[idx] = tc_args.get(idx, "") + fn.arguments
                    yield LLMStreamChunk(
                        content="",
                        finish_reason=finish_reason,
                        metadata={
                            "provider": "litellm",
                            "base_url": self.base_url,
                            "model": model,
                            "is_tool_call": True,
                        },
                    )

            # Surface real usage before the tool-calls sentinel: agent loops break
            # on the sentinel, so anything yielded after it would never be seen.
            if usage_final:
                yield LLMStreamChunk(
                    content="",
                    metadata={"provider": "litellm", "model": model, "usage": usage_final},
                )

            if has_tool_calls and tc_names:
                assembled: list = []
                for idx in sorted(tc_names.keys()):
                    try:
                        args = _json.loads(tc_args.get(idx, "{}"))
                    except Exception:
                        args = {}
                    assembled.append(
                        {"id": tc_ids.get(idx), "name": tc_names[idx], "arguments": args}
                    )
                if assembled:
                    yield LLMStreamChunk(
                        content="",
                        finish_reason="tool_calls",
                        metadata={
                            "provider": "litellm",
                            "base_url": self.base_url,
                            "model": model,
                            "is_tool_call": True,
                            "tool_calls": assembled,
                        },
                    )

        except Exception as e:
            logger.error(f"LiteLLM streaming error: {e}")
            raise

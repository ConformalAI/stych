"""
LLM Inference module with OpenRouter, LiteLLM, and Google AI support.
Includes Langfuse tracking for all LLM calls.
"""

import json
import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator, Dict, List, Optional

from google import genai
from openai import AsyncOpenAI

from llm.langfuse_client import get_langfuse_tracker
from llm.litellm_provider import LiteLLMProvider
from llm.models import LLMRequest, LLMResponse, LLMStreamChunk, Message, usage_to_dict
from llm.prompt_cache import apply_cache_control

logger = logging.getLogger(__name__)


def _safe_tool_call_args(raw: Optional[str], tool_name: str) -> Dict[str, Any]:
    """
    Parse a tool-call's ``arguments`` JSON without letting a malformed payload kill
    the whole request. Models occasionally emit invalid JSON for large multi-line
    arguments (e.g. SQL); coercing to ``{}`` lets the agent loop surface a clear,
    recoverable tool error instead of crashing the run.
    """
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        # Most often this is an output-token truncation (finish_reason="length")
        # that cut the arguments mid-JSON. The ReAct loop inspects finish_reason
        # to turn this into a recoverable instruction rather than a silent failure.
        logger.warning(
            "Malformed/truncated tool-call arguments for tool '%s' (%d chars); coercing to {}",
            tool_name,
            len(raw or ""),
        )
        return {}


# Per-request model override set by the route handler before delegating to the orchestrator.
# Using a ContextVar keeps this thread/task-safe in asyncio without touching the request object.
_request_model_override: ContextVar[Optional[str]] = ContextVar(
    "_request_model_override", default=None
)


class LLMProvider:
    """Base LLM provider interface."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate response from LLM."""
        raise NotImplementedError

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Generate streaming response from LLM."""
        raise NotImplementedError


class OpenRouterProvider(LLMProvider):
    """OpenRouter LLM provider using OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        default_model: str = "anthropic/claude-3.5-sonnet",
        fallback_models: Optional[List[str]] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model
        self.fallback_models = fallback_models or []

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=60.0,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate response using OpenRouter."""
        model = request.model or self.default_model

        try:
            create_kwargs: Dict[str, Any] = dict(
                model=model,
                messages=apply_cache_control(
                    [msg.model_dump(exclude_none=True) for msg in request.messages], model
                ),
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stream=False,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
                extra_body=request.provider_options or {},
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
                    "provider": "openrouter",
                    "tool_calls": tool_calls if tool_calls else None,
                },
            )

        except Exception as e:
            logger.error(f"OpenRouter API error with model {model}: {e}")

            # Try fallback models
            for fallback_model in self.fallback_models:
                try:
                    logger.info(f"Trying fallback model: {fallback_model}")
                    request.model = fallback_model
                    return await self.generate(request)
                except Exception as fallback_error:
                    logger.error(f"Fallback model {fallback_model} failed: {fallback_error}")
                    continue

            raise Exception(f"All models failed. Last error: {e}")

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Generate streaming response using OpenRouter.

        Each yielded chunk has ``metadata["is_tool_call"]`` set to False for
        plain-text tokens and True for tool-call deltas.  After all chunks,
        a final sentinel chunk is yielded with ``metadata["tool_calls"]``
        containing the assembled list when the model issued a tool call.
        """
        model = request.model or self.default_model

        try:
            create_kwargs: Dict[str, Any] = dict(
                model=model,
                messages=apply_cache_control(
                    [msg.model_dump(exclude_none=True) for msg in request.messages], model
                ),
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stream=True,
                stop=request.stop,
                presence_penalty=request.presence_penalty,
                frequency_penalty=request.frequency_penalty,
                extra_body=request.provider_options or {},
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

            # Accumulators for streaming tool-call arguments
            usage_final: Optional[Dict[str, int]] = None
            tc_names: Dict[int, str] = {}
            tc_args: Dict[int, str] = {}
            tc_ids: Dict[int, str] = {}
            has_tool_calls = False

            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_final = usage_to_dict(chunk.usage)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                # Reasoning models (DeepSeek-R1, Qwen via OpenRouter) surface
                # chain-of-thought via `reasoning_content` on the delta.
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    yield LLMStreamChunk(
                        content=rc,
                        finish_reason=finish_reason,
                        metadata={
                            "provider": "openrouter",
                            "model": model,
                            "is_tool_call": False,
                            "is_thinking": True,
                        },
                    )

                # Plain-text content token
                if delta.content:
                    yield LLMStreamChunk(
                        content=delta.content,
                        finish_reason=finish_reason,
                        metadata={
                            "provider": "openrouter",
                            "model": model,
                            "is_tool_call": False,
                            "is_thinking": False,
                        },
                    )

                # Tool-call delta — accumulate name + arguments fragments
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
                    # Signal to caller: tool-call in progress, no content to display
                    yield LLMStreamChunk(
                        content="",
                        finish_reason=finish_reason,
                        metadata={
                            "provider": "openrouter",
                            "model": model,
                            "is_tool_call": True,
                        },
                    )

            # Surface real usage before the tool-calls sentinel: agent loops break
            # on the sentinel, so anything yielded after it would never be seen.
            if usage_final:
                yield LLMStreamChunk(
                    content="",
                    metadata={"provider": "openrouter", "model": model, "usage": usage_final},
                )

            # Final sentinel carrying the fully assembled tool calls.
            # Only emit when we actually have at least one named tool call —
            # an empty list sentinel causes the orchestrator to break early.
            if has_tool_calls and tc_names:
                assembled: list = []
                for idx in sorted(tc_names.keys()):
                    try:
                        args = json.loads(tc_args.get(idx, "{}"))
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
                            "provider": "openrouter",
                            "model": model,
                            "is_tool_call": True,
                            "tool_calls": assembled,
                        },
                    )

        except Exception as e:
            logger.error(f"OpenRouter streaming error: {e}")
            raise


class GoogleProvider(LLMProvider):
    """Google Generative AI provider."""

    def __init__(
        self,
        api_key: str,
        default_model: str = "gemini-1.5-pro",
    ):
        self.api_key = api_key
        self.default_model = default_model
        # Use the Client class instead of configure
        self.client = genai.Client(api_key=api_key)

    def _convert_messages(self, messages: List[Message]) -> str:
        """Convert messages to Google format - just concatenate as text."""
        # The new google-genai API accepts simple string content
        # Concatenate all messages into a single prompt
        return "\n".join([f"{msg.role}: {msg.content}" for msg in messages])

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate response using Google Gemini."""
        model_name = request.model or self.default_model

        try:
            # Convert messages to simple text
            prompt = self._convert_messages(request.messages)

            # Generate response using the client
            response = await self.client.aio.models.generate_content(
                model=model_name,
                contents=prompt,  # Send as simple string
                config=genai.types.GenerateContentConfig(
                    temperature=request.temperature,
                    max_output_tokens=request.max_tokens,
                    top_p=request.top_p,
                ),
            )

            return LLMResponse(
                content=response.text or "",  # Default to empty string if None
                model=model_name,
                usage={
                    "prompt_tokens": (
                        (getattr(response.usage_metadata, "prompt_token_count", None) or 0)
                        if response.usage_metadata
                        else 0
                    ),
                    "completion_tokens": (
                        (getattr(response.usage_metadata, "candidates_token_count", None) or 0)
                        if response.usage_metadata
                        else 0
                    ),
                    "total_tokens": (
                        (getattr(response.usage_metadata, "total_token_count", None) or 0)
                        if response.usage_metadata
                        else 0
                    ),
                },
                finish_reason=(
                    response.candidates[0].finish_reason.name
                    if response.candidates and len(response.candidates) > 0
                    else "stop"
                ),
                metadata={"provider": "google"},
            )

        except Exception as e:
            logger.error(f"Google API error: {e}")
            raise

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Generate streaming response using Google Gemini."""
        model_name = request.model or self.default_model

        try:
            prompt = self._convert_messages(request.messages)

            # Use client for streaming
            response = self.client.aio.models.generate_content_stream(
                model=model_name,
                contents=prompt,  # Send as simple string
                config=genai.types.GenerateContentConfig(
                    temperature=request.temperature,
                    max_output_tokens=request.max_tokens,
                    top_p=request.top_p,
                ),
            )

            async for chunk in response:
                if chunk.text:
                    yield LLMStreamChunk(
                        content=chunk.text,
                        metadata={"provider": "google", "model": model_name},
                    )

        except Exception as e:
            logger.error(f"Google streaming error: {e}")
            raise


class PrivateLLMProvider(LLMProvider):
    """Private/self-hosted LLM provider."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url
        self.model = model
        self.client = AsyncOpenAI(
            api_key=api_key or "dummy",
            base_url=base_url,
            timeout=60.0,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate response using private LLM."""
        try:
            create_kwargs: Dict[str, Any] = dict(
                model=request.model or self.model,
                messages=apply_cache_control(
                    [msg.model_dump(exclude_none=True) for msg in request.messages],
                    request.model or self.model,
                ),
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stream=False,
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
                    "id": getattr(tc, "id", None),
                    "name": tc.function.name,
                    "arguments": _safe_tool_call_args(tc.function.arguments, tc.function.name),
                }
                for tc in tool_calls_raw
            ]
            return LLMResponse(
                content=choice.message.content or "",
                model=response.model,
                usage={
                    "prompt_tokens": (response.usage.prompt_tokens if response.usage else 0),
                    "completion_tokens": (
                        response.usage.completion_tokens if response.usage else 0
                    ),
                    "total_tokens": (response.usage.total_tokens if response.usage else 0),
                },
                finish_reason=choice.finish_reason,
                metadata={
                    "provider": "private",
                    "tool_calls": tool_calls if tool_calls else None,
                },
            )

        except Exception as e:
            logger.error(f"Private LLM error: {e}")
            raise

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Generate streaming response using private LLM."""
        try:
            create_kwargs: Dict[str, Any] = dict(
                model=request.model or self.model,
                messages=apply_cache_control(
                    [msg.model_dump(exclude_none=True) for msg in request.messages],
                    request.model or self.model,
                ),
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stream=True,
            )
            if request.tools:
                create_kwargs["tools"] = request.tools
                if request.tool_choice is not None:
                    create_kwargs["tool_choice"] = request.tool_choice

            stream = await self.client.chat.completions.create(**create_kwargs)

            tc_names: Dict[int, str] = {}
            tc_args: Dict[int, str] = {}
            tc_ids: Dict[int, str] = {}
            has_tool_calls = False

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if delta.content:
                    yield LLMStreamChunk(
                        content=delta.content,
                        finish_reason=finish_reason,
                        metadata={"provider": "private", "is_tool_call": False},
                    )

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
                        metadata={"provider": "private", "is_tool_call": True},
                    )

            if has_tool_calls and tc_names:
                assembled: list = []
                for idx in sorted(tc_names.keys()):
                    try:
                        args = json.loads(tc_args.get(idx, "{}"))
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
                            "provider": "private",
                            "is_tool_call": True,
                            "tool_calls": assembled,
                        },
                    )

        except Exception as e:
            logger.error(f"Private LLM streaming error: {e}")
            raise


class LLMRouter:
    """Routes LLM requests to appropriate provider."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.provider_name = config.get("provider", "openrouter")
        self.provider = self._initialize_provider()

    def _initialize_provider(self) -> LLMProvider:
        """Initialize the configured LLM provider."""
        if self.provider_name == "litellm":
            litellm_config = self.config.get("litellm", {})
            return LiteLLMProvider(
                base_url=litellm_config.get("base_url", "http://localhost:4000"),
                api_key=litellm_config.get("api_key"),
                default_model=litellm_config.get("default_model", "gpt-4"),
                fallback_models=litellm_config.get("fallback_models", []),
            )

        elif self.provider_name == "openrouter":
            openrouter_config = self.config.get("openrouter", {})
            return OpenRouterProvider(
                api_key=openrouter_config.get("api_key"),
                base_url=openrouter_config.get("base_url", "https://openrouter.ai/api/v1"),
                default_model=openrouter_config.get("default_model", "anthropic/claude-3.5-sonnet"),
                fallback_models=openrouter_config.get("fallback_models", []),
            )

        elif self.provider_name == "google":
            google_config = self.config.get("google", {})
            return GoogleProvider(
                api_key=google_config.get("api_key"),
                default_model=google_config.get("default_model", "gemini-1.5-pro"),
            )

        elif self.provider_name == "private":
            private_config = self.config.get("private", {})
            return PrivateLLMProvider(
                base_url=private_config.get("base_url"),
                model=private_config.get("model"),
            )

        else:
            raise ValueError(f"Unknown provider: {self.provider_name}")

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Route request to configured provider with Langfuse tracking."""
        logger.info(f"Generating with provider: {self.provider_name}")

        # Apply per-request model override when set (e.g. from user's preferred_model).
        model_override = _request_model_override.get()
        if model_override:
            request = request.model_copy(update={"model": model_override})

        # Track start time for latency
        start_time = time.time()

        # Generate response
        response = await self.provider.generate(request)

        # Calculate latency
        latency_ms = (time.time() - start_time) * 1000

        # Log to Langfuse if enabled
        langfuse_tracker = get_langfuse_tracker()
        if langfuse_tracker:
            try:
                langfuse_tracker.log_llm_call(
                    agent_name=(
                        request.metadata.get("agent_name", "unknown")
                        if request.metadata
                        else "unknown"
                    ),
                    model=response.model,
                    provider=response.metadata.get("provider", self.provider_name),
                    input_messages=[msg.model_dump(exclude_none=True) for msg in request.messages],
                    output=response.content,
                    latency_ms=latency_ms,
                    usage=response.usage,
                    session_id=(request.metadata.get("session_id") if request.metadata else None),
                )
            except Exception as e:
                logger.error(f"Error logging to Langfuse: {e}")

        return response

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Route streaming request to configured provider with Langfuse tracking."""
        logger.info(f"Streaming with provider: {self.provider_name}")

        # Apply per-request model override when set.
        model_override = _request_model_override.get()
        if model_override:
            request = request.model_copy(update={"model": model_override})

        # Track start time
        start_time = time.time()

        # Collect full response + real usage (surfaced by providers on a
        # metadata["usage"] chunk just before any tool-calls sentinel).
        full_response = ""
        stream_usage: Optional[Dict[str, int]] = None

        try:
            async for chunk in self.provider.generate_stream(request):
                meta_usage = (chunk.metadata or {}).get("usage")
                if meta_usage:
                    stream_usage = meta_usage
                if chunk.content:
                    full_response += chunk.content
                yield chunk
        finally:
            # Runs even when the consumer breaks early (agent loops stop at the
            # tool-calls sentinel, which closes this generator) — usage must
            # still be recorded or streamed tool turns are never billed.
            latency_ms = (time.time() - start_time) * 1000

            usage = stream_usage
            if not usage:
                # Provider gave no usage — estimate with the same tiktoken-based
                # counter the memory/compaction budgets use, so billed and
                # budgeted token math agree; "estimated": 1 marks these rows.
                from memory.token_counter import count as _count_tokens

                est_prompt = sum(_count_tokens(m.content or "") for m in request.messages)
                est_completion = _count_tokens(full_response)
                usage = {
                    "prompt_tokens": est_prompt,
                    "completion_tokens": est_completion,
                    "total_tokens": est_prompt + est_completion,
                    "estimated": 1,
                }

            langfuse_tracker = get_langfuse_tracker()
            if langfuse_tracker:
                try:
                    langfuse_tracker.log_llm_call(
                        agent_name=(
                            request.metadata.get("agent_name", "unknown")
                            if request.metadata
                            else "unknown"
                        ),
                        model=request.model or "unknown",
                        provider=self.provider_name,
                        input_messages=[
                            msg.model_dump(exclude_none=True) for msg in request.messages
                        ],
                        output=full_response,
                        latency_ms=latency_ms,
                        usage=usage,
                        session_id=(
                            request.metadata.get("session_id") if request.metadata else None
                        ),
                    )
                except Exception as e:
                    logger.error(f"Error logging stream to Langfuse: {e}")

    async def health_check(self) -> Dict[str, Any]:
        """
        Check health of all configured LLM providers.
        Returns status of LiteLLM, OpenRouter, and Google AI.
        """
        results = {
            "litellm": {"status": "unconfigured", "error": None},
            "openrouter": {"status": "unconfigured", "error": None},
            "google": {"status": "unconfigured", "error": None},
        }

        # Check LiteLLM
        litellm_config = self.config.get("litellm", {})
        litellm_url = litellm_config.get("base_url")

        if litellm_url:
            try:
                provider = LiteLLMProvider(
                    base_url=litellm_url,
                    api_key=litellm_config.get("api_key"),
                    default_model=litellm_config.get("default_model", "gpt-4"),
                )

                # Send a minimal test request
                test_request = LLMRequest(
                    messages=[Message(role="user", content="ping")],
                    max_tokens=5,
                    temperature=0.0,
                )

                response = await provider.generate(test_request)
                results["litellm"]["status"] = "healthy"
                results["litellm"]["model"] = response.model
                logger.info("✅ LiteLLM: Connected successfully")

            except Exception as e:
                error_msg = str(e)
                results["litellm"]["status"] = "error"
                results["litellm"]["error"] = error_msg

                if "Connection refused" in error_msg or "connect" in error_msg.lower():
                    logger.error(f"❌ LiteLLM: Cannot connect to {litellm_url}")
                else:
                    logger.error(f"❌ LiteLLM: {error_msg}")
        else:
            logger.warning("⚠️  LiteLLM: Base URL not configured")

        # Check OpenRouter
        openrouter_config = self.config.get("openrouter", {})
        openrouter_key = openrouter_config.get("api_key")

        if openrouter_key and openrouter_key != "your_openrouter_api_key_here":
            try:
                provider = OpenRouterProvider(
                    api_key=openrouter_key,
                    base_url=openrouter_config.get("base_url", "https://openrouter.ai/api/v1"),
                    default_model=openrouter_config.get(
                        "default_model", "anthropic/claude-3-haiku"
                    ),
                )

                # Send a minimal test request
                test_request = LLMRequest(
                    messages=[Message(role="user", content="ping")],
                    max_tokens=5,
                    temperature=0.0,
                )

                response = await provider.generate(test_request)
                results["openrouter"]["status"] = "healthy"
                results["openrouter"]["model"] = response.model
                logger.info("✅ OpenRouter: Connected successfully")

            except Exception as e:
                error_msg = str(e)
                results["openrouter"]["status"] = "error"
                results["openrouter"]["error"] = error_msg

                if "401" in error_msg or "authentication" in error_msg.lower():
                    logger.error("❌ OpenRouter: Authentication failed - check OPENROUTER_API_KEY")
                elif "502" in error_msg:
                    logger.error("❌ OpenRouter: Service unavailable (502)")
                else:
                    logger.error(f"❌ OpenRouter: {error_msg}")
        else:
            logger.warning("⚠️  OpenRouter: API key not configured")

        # Check Google AI
        google_config = self.config.get("google", {})
        google_key = google_config.get("api_key")

        if google_key and google_key != "your_google_api_key_here":
            try:
                provider = GoogleProvider(
                    api_key=google_key,
                    default_model=google_config.get("default_model", "gemini-1.5-flash"),
                )

                # Send a minimal test request
                test_request = LLMRequest(
                    messages=[Message(role="user", content="ping")],
                    max_tokens=5,
                    temperature=0.0,
                )

                response = await provider.generate(test_request)
                results["google"]["status"] = "healthy"
                results["google"]["model"] = response.model
                logger.info("✅ Google AI: Connected successfully")

            except Exception as e:
                error_msg = str(e)
                results["google"]["status"] = "error"
                results["google"]["error"] = error_msg

                if "API_KEY_INVALID" in error_msg or "authentication" in error_msg.lower():
                    logger.error("❌ Google AI: Authentication failed - check GOOGLE_API_KEY")
                else:
                    logger.error(f"❌ Google AI: {error_msg}")
        else:
            logger.warning("⚠️  Google AI: API key not configured")

        return results

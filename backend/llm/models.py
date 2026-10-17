"""
Pydantic models for LLM requests and responses.
"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class Message(BaseModel):
    """Chat message model.

    Supports native tool calling (OpenAI / Anthropic shape):
    - An ``assistant`` message may carry ``tool_calls`` (a list of
      ``{"id", "type": "function", "function": {"name", "arguments"}}`` dicts)
      and a possibly-empty/None ``content``.
    - A ``tool`` message carries the result of one tool call, correlated back to
      the assistant's call via ``tool_call_id``.

    All new fields are optional; with ``model_dump(exclude_none=True)`` a plain
    text message serializes identically to before, so single-shot LLM calls are
    unaffected.
    """

    role: Literal["system", "user", "assistant", "function", "tool"]
    # content is Optional: an assistant message that only makes tool calls may
    # have null content (OpenAI-compatible APIs accept this).
    content: Optional[str] = None
    name: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    # Native tool-calling round-trip fields.
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class LLMRequest(BaseModel):
    """LLM inference request model."""

    messages: List[Message]
    model: Optional[str] = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=4096, gt=0)
    top_p: Optional[float] = Field(default=1.0, ge=0.0, le=1.0)
    stream: bool = False
    stop: Optional[List[str]] = None
    presence_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=0.0, ge=-2.0, le=2.0)

    # Provider-specific options
    provider_options: Optional[Dict[str, Any]] = None

    # Native tool calling (OpenAI tool schema format)
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

    # Tracking metadata (for Langfuse)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """LLM inference response model."""

    content: str
    model: str
    usage: Dict[str, int] = Field(default_factory=dict)
    finish_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class LLMStreamChunk(BaseModel):
    """Streaming response chunk."""

    content: str
    finish_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


def usage_to_dict(usage: Any) -> Dict[str, int]:
    """
    Normalize an OpenAI-compatible usage object into ``LLMResponse.usage``.

    Also surfaces Anthropic prompt-cache accounting when the provider reports it
    (``prompt_tokens_details.cached_tokens`` or ``cache_read_input_tokens``, plus
    ``cache_creation_input_tokens``) so telemetry can verify cache hit rates.
    """
    if usage is None:
        return {}
    out: Dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = getattr(usage, key, None)
        if val is not None:
            out[key] = int(val)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if cached is None:
        cached = getattr(usage, "cache_read_input_tokens", None)
    if cached is not None:
        out["cached_tokens"] = int(cached)
    creation = getattr(usage, "cache_creation_input_tokens", None)
    if creation is not None:
        out["cache_creation_tokens"] = int(creation)
    return out


class ModelInfo(BaseModel):
    """Model information."""

    id: str
    name: str
    provider: str
    context_length: int
    pricing: Optional[Dict[str, float]] = None
    capabilities: List[str] = Field(default_factory=list)

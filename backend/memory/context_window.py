"""
Model context-window registry and token budget helpers.

Keys are lowercase substrings — get_context_window() does a substring
match so "openrouter/anthropic/claude-sonnet-4.5" → 200 000.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Ordered so longer/more-specific substrings come first.
_MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    # Claude 4 family
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    # Claude 3.x
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-3-opus", 200_000),
    ("claude-3-sonnet", 200_000),
    ("claude-3-haiku", 200_000),
    # Gemini
    ("gemini-2.5-pro", 1_000_000),
    ("gemini-2.5-flash", 1_000_000),
    ("gemini-2.0-flash", 128_000),
    ("gemini-1.5-pro", 2_000_000),
    ("gemini-1.5-flash", 1_000_000),
    # OpenAI
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5-turbo", 16_385),
]

_DEFAULT_CONTEXT_WINDOW = 128_000

# Max *output* (completion) tokens per model — distinct from the context window
# above. Same ordered-substring matching; longer/more-specific keys first.
_MODEL_OUTPUT_LIMITS: list[tuple[str, int]] = [
    # Claude 4 family
    ("claude-opus-4", 64_000),
    ("claude-sonnet-4", 64_000),
    ("claude-haiku-4", 64_000),
    # Claude 3.x
    ("claude-3-5-sonnet", 8_192),
    ("claude-3-5-haiku", 8_192),
    ("claude-3-opus", 4_096),
    ("claude-3-sonnet", 4_096),
    ("claude-3-haiku", 4_096),
    # Gemini
    ("gemini-2.5-pro", 65_536),
    ("gemini-2.5-flash", 65_536),
    ("gemini-2.0-flash", 8_192),
    ("gemini-1.5-pro", 8_192),
    ("gemini-1.5-flash", 8_192),
    # OpenAI
    ("gpt-4o", 16_384),
    ("gpt-4-turbo", 4_096),
    ("gpt-4", 8_192),
    ("gpt-3.5-turbo", 4_096),
]

_DEFAULT_OUTPUT_LIMIT = 8_192
# Hard ceiling on a single ReAct-step's output budget. opencode uses 32_000;
# 16k comfortably covers a long summary plus reasoning while bounding cost.
DEFAULT_OUTPUT_CAP = 16_000


def get_context_window(model: Optional[str]) -> int:
    """Return the context window size (in tokens) for *model*."""
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    lower = model.lower()
    for substring, size in _MODEL_CONTEXT_WINDOWS:
        if substring in lower:
            return size
    logger.debug(
        "Unknown model %r — using default context window %d",
        model,
        _DEFAULT_CONTEXT_WINDOW,
    )
    return _DEFAULT_CONTEXT_WINDOW


def get_max_output_tokens(model: Optional[str], cap: int = DEFAULT_OUTPUT_CAP) -> int:
    """Return the per-request output-token budget for *model*, clamped to *cap*.

    Mirrors opencode's ``min(model.limit.output, OUTPUT_TOKEN_MAX)``: large enough
    that a tool call carrying a long file ``content`` argument is not truncated
    mid-JSON, while never exceeding the model's own output ceiling.
    """
    limit = _DEFAULT_OUTPUT_LIMIT
    if model:
        lower = model.lower()
        for substring, size in _MODEL_OUTPUT_LIMITS:
            if substring in lower:
                limit = size
                break
    return min(limit, cap)


def get_token_budget(model: Optional[str], fraction: float = 0.70) -> int:
    """
    Return the token budget for conversation history.

    *fraction* is the share of the context window reserved for memory
    (default 0.70 = 70 %).  The caller is responsible for subtracting
    its own overhead (system prompt, dataset context, etc.).
    """
    return int(get_context_window(model) * fraction)

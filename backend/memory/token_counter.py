"""
Token counting utilities.

Primary: tiktoken cl100k_base (reasonable approximation for Claude + Gemini).
Fallback: len(text) // 4 when tiktoken is unavailable.
"""

from __future__ import annotations

import logging
from typing import Dict, Sequence

logger = logging.getLogger(__name__)

# Loaded once at import time; None if tiktoken is not installed.
_encoding = None

try:
    import tiktoken

    _encoding = tiktoken.get_encoding("cl100k_base")
    logger.debug("tiktoken loaded (cl100k_base)")
except Exception:
    logger.warning(
        "tiktoken not available — falling back to char/4 token estimation. "
        "Install tiktoken for more accurate context-window budgeting."
    )


def count(text: str) -> int:
    """Return the estimated token count for *text*."""
    if not text:
        return 0
    if _encoding is not None:
        return len(_encoding.encode(text))
    return max(1, len(text) // 4)


def count_messages(messages: Sequence[Dict[str, str]]) -> int:
    """
    Return the estimated token count for a list of role/content dicts.

    Adds a small per-message overhead (4 tokens) to approximate the
    formatting tokens that chat APIs add around each message.
    """
    total = 0
    for msg in messages:
        total += count(msg.get("content", ""))
        total += count(msg.get("role", ""))
        total += 4  # chat-message framing overhead
    return total


def estimate_overhead(system_prompt: str = "", extra_context: str = "") -> int:
    """
    Estimate how many tokens the fixed parts of a prompt consume so
    callers can pass an accurate *overhead_tokens* value to get_window().
    """
    return count(system_prompt) + count(extra_context) + 8  # small buffer

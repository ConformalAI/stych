"""
Anthropic prompt-caching breakpoints for OpenAI-compatible providers.

The append-only native-tool transcript (react_engine/loop.py) produces a stable,
cacheable prefix on every step of an agent loop. This module turns that
structural property into an actual cost/latency win by placing Anthropic
``cache_control`` breakpoints on the prefix.

How it works
------------
Anthropic caches by *prefix*: a breakpoint on a block caches everything from the
start of the request up to and including that block. The request order is
``tools → system → messages``, so:

* A breakpoint on the **system** message caches ``tools + system`` — the part of
  the request that is static for the whole loop (the agent's instructions and
  tool schemas). This is the big, always-present win.
* A breakpoint on the **last message** caches the accumulated conversation
  prefix. As the transcript grows step to step, each step writes a cache entry
  for its prefix and reads the previous one, so re-processing is ~O(n) across the
  loop instead of O(n²).

Two breakpoints, well within Anthropic's limit of four.

Delivery through OpenAI-compatible endpoints
--------------------------------------------
Both LiteLLM and OpenRouter accept ``cache_control`` when a message's ``content``
is a list of content parts (``[{"type": "text", "text": ..., "cache_control":
{"type": "ephemeral"}}]``) and translate it to Anthropic's native cache_control.
So a breakpoint = rewrapping a message's string content into that structured
form. Non-Anthropic models keep plain-string content (this is a no-op for them).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_EPHEMERAL = {"type": "ephemeral"}


def model_supports_caching(model: Optional[str]) -> bool:
    """Only Anthropic (Claude) models support prompt-cache breakpoints here."""
    if not model:
        return False
    m = model.lower()
    return "claude" in m or "anthropic" in m


def _with_cache_control(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *msg* with a cache_control breakpoint on its content.

    String content is rewrapped as a single text part; list content gets the
    breakpoint on its last text part. Messages with no text content (e.g. an
    assistant turn that only carries tool_calls) are returned unchanged — they
    cannot host a breakpoint and never sit at a generation boundary anyway.
    """
    content = msg.get("content")
    if isinstance(content, str) and content:
        new = dict(msg)
        new["content"] = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
        return new
    if isinstance(content, list) and content:
        new = dict(msg)
        parts = [dict(p) for p in content]
        for part in reversed(parts):
            if isinstance(part, dict) and part.get("type") == "text":
                part["cache_control"] = _EPHEMERAL
                break
        new["content"] = parts
        return new
    return msg


def apply_cache_control(
    messages: List[Dict[str, Any]], model: Optional[str]
) -> List[Dict[str, Any]]:
    """
    Place cache breakpoints on the static prefix (last system message) and the
    rolling conversation prefix (last message). No-op for non-Anthropic models
    and empty message lists. Returns a new list; inputs are not mutated.
    """
    if not messages or not model_supports_caching(model):
        return messages

    out = [dict(m) for m in messages]
    targets = set()

    # Static prefix: the last system message (caches tools + system).
    last_system = next(
        (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "system"), None
    )
    if last_system is not None:
        targets.add(last_system)

    # Rolling prefix: the last message overall.
    targets.add(len(out) - 1)

    for i in targets:
        out[i] = _with_cache_control(out[i])
    return out

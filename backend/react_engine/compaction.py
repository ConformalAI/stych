"""
Token-budget compaction for append-only agent transcripts.

Append-only native-tool transcripts grow every step. On a long loop with large
tool observations the transcript can approach the model's context window. This
module is the safety net: when the transcript exceeds its budget, it *clears
the content* of older tool observations while preserving structure.

Why content-clearing and not removal
------------------------------------
OpenAI/Anthropic require every assistant ``tool_calls`` entry to be paired with a
following ``tool`` result (matched by ``tool_call_id``). Dropping a message would
break that pairing and make the request invalid. So compaction never removes
messages — it replaces the *content* of old ``tool`` results with an explicit
"cleared" marker (the model can re-run the tool if it still needs the data),
leaving the system prompt, the original task, the tool-call framing, and the
most recent steps verbatim. A whole-message marker is honest; the previous
mid-string truncation left garbled fragments that looked like real data.

Hysteresis
----------
Compaction fires once per threshold crossing and clears down to
``COMPACT_TO_FRACTION`` of the budget in a single pass. Without the low-water
mark, a transcript sitting at the budget would be rewritten on *every* step as
the keep-recent boundary slides, invalidating the prompt-cache prefix each time
— exactly when the transcript is at its most expensive.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from llm.models import Message
from memory.context_window import get_context_window

logger = logging.getLogger(__name__)

# Token estimate helpers live in memory.token_counter; import the primitive.
from memory.token_counter import count as _count_tokens  # noqa: E402

# Defaults — the budget is min(fraction * context window, absolute cap).
# The fraction targets ~60-70% of the window; the absolute cap keeps
# huge-window models (Gemini 1M/2M) from re-sending hundreds of K tokens on
# every step. On a 200K Claude window: min(0.70 × 200K, 120K) = 120K (60%).
DEFAULT_BUDGET_FRACTION = 0.70
DEFAULT_MAX_TRANSCRIPT_TOKENS = 120_000
# Low-water mark: one compaction pass clears down to this fraction of the
# budget, so the prefix then stays byte-stable (cache-friendly) until the
# transcript grows back over the budget.
COMPACT_TO_FRACTION = 0.50
DEFAULT_KEEP_RECENT_MESSAGES = 6  # ~3 assistant/tool step pairs kept verbatim
COMPACTED_ASSISTANT_CHARS = 200  # old assistant prose shrinks to this
CLEARED_TOOL_CONTENT = (
    "[old tool result cleared to stay within the context budget — "
    "re-run the tool if you need this data again]"
)
_COMPACT_MARKER = "\n…[older content compacted to fit the context window]"


def estimate_transcript_tokens(transcript: List[Message]) -> int:
    """Approximate the token cost of a transcript, including tool-call arguments."""
    return sum(_message_tokens(m) for m in transcript)


def _message_tokens(m: Message) -> int:
    total = _count_tokens(m.content or "")
    total += _count_tokens(m.role)
    total += 4  # per-message framing overhead
    if m.tool_calls:
        for tc in m.tool_calls:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            total += _count_tokens(str(fn.get("name", "")))
            total += _count_tokens(str(fn.get("arguments", "")))
            total += 8  # tool-call framing overhead
    return total


def _shrink(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + _COMPACT_MARKER


def maybe_compact(
    transcript: List[Message],
    model: str | None,
    *,
    budget_fraction: float = DEFAULT_BUDGET_FRACTION,
    keep_recent_messages: int = DEFAULT_KEEP_RECENT_MESSAGES,
    max_transcript_tokens: int | None = None,
) -> Tuple[List[Message], bool]:
    """
    Return ``(transcript, compacted)``.

    When the transcript fits within the budget the original list is returned
    unchanged (``compacted=False``). When it does not, one pass clears old
    ``tool`` observations (oldest first) — and, only if that is not enough,
    shrinks old ``assistant`` prose — until the transcript fits the low-water
    mark (``COMPACT_TO_FRACTION`` × budget). System messages and the first user
    message (the task) are always preserved verbatim, as are the last
    ``keep_recent_messages`` messages.
    """
    if not transcript:
        return transcript, False

    budget = min(
        int(get_context_window(model) * budget_fraction),
        max_transcript_tokens or DEFAULT_MAX_TRANSCRIPT_TOKENS,
    )
    per_message = [_message_tokens(m) for m in transcript]
    total = sum(per_message)
    if total <= budget:
        return transcript, False

    target = int(budget * COMPACT_TO_FRACTION)

    # Index of the first user message (the task) — preserved verbatim.
    first_user_idx = next((i for i, m in enumerate(transcript) if m.role == "user"), None)
    # Boundary: everything from here to the end is kept verbatim.
    recent_start = max(0, len(transcript) - keep_recent_messages)

    def _compactable(i: int, m: Message) -> bool:
        return not (m.role == "system" or i == first_user_idx or i >= recent_start)

    out: List[Message] = list(transcript)
    shrunk_any = False

    # Pass 1: clear whole old tool observations, oldest first.
    for i, m in enumerate(transcript):
        if total <= target:
            break
        if not _compactable(i, m) or m.role != "tool":
            continue
        if not m.content or m.content == CLEARED_TOOL_CONTENT:
            continue
        replaced = m.model_copy(update={"content": CLEARED_TOOL_CONTENT})
        new_tokens = _message_tokens(replaced)
        if new_tokens >= per_message[i]:
            continue  # already tiny; clearing would not save anything
        out[i] = replaced
        total -= per_message[i] - new_tokens
        per_message[i] = new_tokens
        shrunk_any = True

    # Pass 2 (rare): still over target — shrink old assistant prose.
    if total > target:
        for i, m in enumerate(transcript):
            if total <= target:
                break
            if not _compactable(i, m) or m.role != "assistant":
                continue
            if not m.content or len(m.content) <= COMPACTED_ASSISTANT_CHARS:
                continue
            # Preserve tool_calls intact; only shrink the prose/"thinking".
            replaced = m.model_copy(
                update={"content": _shrink(m.content, COMPACTED_ASSISTANT_CHARS)}
            )
            new_tokens = _message_tokens(replaced)
            if new_tokens >= per_message[i]:
                continue
            out[i] = replaced
            total -= per_message[i] - new_tokens
            per_message[i] = new_tokens
            shrunk_any = True

    if not shrunk_any:
        return transcript, False

    logger.info(
        "[compaction] transcript compacted to ~%d tokens (budget %d, target %d, model=%s)",
        total,
        budget,
        target,
        model or "default",
    )
    return out, True

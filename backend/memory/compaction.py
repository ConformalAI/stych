"""
Cross-turn conversation compaction for single-session short-term memory.

As a session grows, the older turns are folded into an LLM-generated *summary*
instead of being dropped by a rolling window. Only the most recent turns are kept
verbatim; everything older is condensed into a running summary that is re-injected
into the orchestrator prompt.

This operates on ``AgentState.messages`` (the cross-query conversation) and is
distinct from ``react_engine/compaction.py``, which shrinks a *single* query's
native-tool transcript.

State is tracked on ``state.metadata``:
  - SUMMARY_META_KEY           : the running summary string.
  - SUMMARIZED_COUNT_META_KEY  : watermark — number of leading messages already
                                 folded into the summary (an index into the
                                 message list, stable because messages are
                                 append-only).

The module is import-light (no ``agents`` import) so it can be unit-tested in
isolation, mirroring ``react_engine/compaction.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

from llm.models import LLMRequest, Message

from .context_window import get_context_window
from .token_counter import count_messages

if TYPE_CHECKING:  # pragma: no cover
    from agents.state import Message as StateMessage  # noqa: F401

logger = logging.getLogger(__name__)

SUMMARY_META_KEY = "conversation_summary"
SUMMARIZED_COUNT_META_KEY = "conversation_summarized_count"

# Defaults mirror config.yaml (memory.short_term.compaction) so callers that omit
# a config still get sane behaviour.
DEFAULT_TRIGGER_FRACTION = 0.60
DEFAULT_KEEP_RECENT_MESSAGES = 6
DEFAULT_MAX_SUMMARY_TOKENS = 1500

_SUMMARY_SYSTEM_PROMPT = (
    "You are a summarization engine for a conversational data-analysis platform. "
    "You will be given the earlier part of a chat between a user and an AI data "
    "assistant, and (optionally) a prior summary. Produce a single, dense summary "
    "that preserves everything needed to continue the conversation without the raw "
    "messages.\n\n"
    "Respond with TEXT ONLY. Do NOT call any tools. Do NOT ask questions or add "
    "commentary — output only the summary.\n\n"
    "Cover, in this order, omitting any section that has no content:\n"
    "1. Primary intent — what the user is ultimately trying to accomplish.\n"
    "2. Dataset context — dataset name(s), schema/columns, and any cleaning or "
    "transformations already applied. Preserve exact column names and cleaning "
    "operations; these must not be lost.\n"
    "3. Analyses run and key findings — computations, statistics, charts produced, "
    "and their concrete results/numbers.\n"
    "4. Decisions and user preferences stated so far.\n"
    "5. Pending tasks and current work in progress.\n\n"
    "Be concise but do not drop specifics (numbers, column names, filenames). "
    "When a prior summary is provided, merge it with the new messages into one "
    "coherent summary — do not simply append."
)


def _to_role_content(msg: Any) -> tuple[str, str]:
    """Extract (role, content) from a state Message or a plain dict."""
    if isinstance(msg, dict):
        return str(msg.get("role", "user")), str(msg.get("content") or "")
    return str(getattr(msg, "role", "user")), str(getattr(msg, "content", "") or "")


def should_compact(
    unsummarized_messages: List[Any],
    model: Optional[str],
    trigger_fraction: float,
) -> bool:
    """
    True when the not-yet-summarized tail exceeds ``trigger_fraction`` of the
    model's context window.
    """
    if not unsummarized_messages:
        return False
    dicts = [{"role": r, "content": c} for r, c in map(_to_role_content, unsummarized_messages)]
    tokens = count_messages(dicts)
    threshold = int(get_context_window(model) * trigger_fraction)
    return tokens > threshold


def _render_messages(messages: List[Any]) -> str:
    lines: List[str] = []
    for msg in messages:
        role, content = _to_role_content(msg)
        lines.append(f"{role.capitalize()}: {content.strip()}")
    return "\n\n".join(lines)


async def compact_conversation(
    prior_summary: str,
    messages_to_summarize: List[Any],
    *,
    llm_router: Any,
    model: Optional[str],
    max_summary_tokens: int = DEFAULT_MAX_SUMMARY_TOKENS,
) -> str:
    """
    Summarize ``messages_to_summarize`` (merged with any ``prior_summary``) into a
    single running summary. Returns the summary text, or ``prior_summary`` on any
    failure (never raises).
    """
    if not messages_to_summarize:
        return prior_summary

    user_parts: List[str] = []
    if prior_summary:
        user_parts.append("Prior summary of the conversation so far:\n" + prior_summary)
    user_parts.append(
        "New messages to fold into the summary:\n" + _render_messages(messages_to_summarize)
    )
    user_content = "\n\n---\n\n".join(user_parts)

    request = LLMRequest(
        messages=[
            Message(role="system", content=_SUMMARY_SYSTEM_PROMPT),
            Message(role="user", content=user_content),
        ],
        model=model,
        temperature=0.3,
        max_tokens=max_summary_tokens,
        tools=[],
        metadata={"agent_name": "memory:compaction"},
    )

    try:
        resp = await llm_router.generate(request)
    except Exception as exc:  # noqa: BLE001 — compaction must never break the turn
        logger.warning("Conversation compaction failed (%s); keeping prior summary", exc)
        return prior_summary

    summary = (getattr(resp, "content", "") or "").strip()
    if not summary:
        logger.warning("Conversation compaction returned empty summary; keeping prior summary")
        return prior_summary
    return summary

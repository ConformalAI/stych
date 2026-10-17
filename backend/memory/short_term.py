"""
Short-term memory: the recent, verbatim tail of the conversation.

Older turns are folded into a running summary by ``memory/compaction.py`` (tracked
via a watermark on ``state.metadata``); this class returns only the messages *after*
that watermark — the recent turns kept verbatim. A hard budget floor trims the tail
newest-first as a safety net so the returned window can never overflow the context
window even when no summary exists yet (e.g. right after a DB reload).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

from .context_window import get_context_window
from .interface import MemoryInterface
from .token_counter import count_messages

if TYPE_CHECKING:
    from agents.state import Message

logger = logging.getLogger(__name__)


class ShortTermMemory(MemoryInterface):
    """
    Recent-tail memory backed by AgentState.messages.

    Args:
        budget_fraction: Hard safety cap — the returned tail never exceeds this
            fraction of the model's context window (minus overhead). Compaction
            normally keeps the tail well under this; the cap only bites when no
            summary exists yet.
    """

    def __init__(self, budget_fraction: float = 0.70) -> None:
        self._budget_fraction = budget_fraction

    # ------------------------------------------------------------------
    # MemoryInterface
    # ------------------------------------------------------------------

    def get_window(
        self,
        messages: List["Message"],
        summarized_count: int = 0,
        overhead_tokens: int = 0,
        model: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        if not messages:
            return []

        # Everything after the watermark is the recent, verbatim tail.
        start = max(0, min(summarized_count, len(messages)))
        tail = messages[start:]
        if not tail:
            return []

        budget = max(0, int(get_context_window(model) * self._budget_fraction) - overhead_tokens)

        # Walk newest-to-oldest, keep messages until the hard budget is exhausted.
        selected: List["Message"] = []
        used = 0
        for msg in reversed(tail):
            as_dict = {"role": msg.role, "content": msg.content}
            cost = count_messages([as_dict])
            if selected and used + cost > budget:
                break  # floor guarantees at least one message survives
            selected.append(msg)
            used += cost
        selected.reverse()

        logger.debug(
            "ShortTermMemory tail: %d/%d messages (watermark=%d) — %d/%d tokens, model=%s",
            len(selected),
            len(tail),
            start,
            used,
            budget,
            model or "default",
        )

        return [{"role": m.role, "content": m.content} for m in selected]

"""
Abstract interface for memory backends.

Both ShortTermMemory and future long-term memory (hindsight) implement this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from agents.state import Message


class MemoryInterface(ABC):
    """
    Contract for any memory backend.

    get_window() is the single method callers use. It accepts the full
    message list from AgentState and returns the recent verbatim tail
    (messages after the compaction watermark) as an LLM-ready slice.
    """

    @abstractmethod
    def get_window(
        self,
        messages: List["Message"],
        summarized_count: int = 0,
        overhead_tokens: int = 0,
        model: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Return LLM-ready message dicts for the recent conversation tail —
        the messages *after* the compaction watermark (*summarized_count*),
        trimmed to fit within the memory budget minus *overhead_tokens*.

        Args:
            messages: Full conversation history from AgentState.messages.
            summarized_count: Number of leading messages already folded into
                the running summary; those are skipped here.
            overhead_tokens: Tokens already committed to the prompt. The
                window will not exceed (budget - overhead_tokens) tokens.
            model: Model identifier used to look up context window size.
                Defaults to the registry fallback when None or unknown.

        Returns:
            List of {"role": ..., "content": ...} dicts in chronological order.
        """
        ...

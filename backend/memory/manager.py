"""
MemoryManager — coordinates short-term and long-term memory.

Usage
-----
    manager = MemoryManager(short_term=ShortTermMemory())

    # Fold older turns into a running summary when history grows (preflight):
    await manager.maybe_compact_conversation(state, messages, llm_router=router, model=model, cfg=cfg)

    # In orchestrator / sub-agent — the recent verbatim tail after the watermark:
    history = manager.get_context_window(
        state.messages, summarized_count=w, overhead_tokens=500, model=model
    )

    # Query long-term memory (Hindsight):
    context = await manager.recall_long_term(query=user_message, user_id=user_id)

    # Persist a turn to long-term memory (fire-and-forget):
    asyncio.create_task(manager.retain_turn(user_message, response, session_id, user_id))
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from helpers.sse import SseEventType

from .compaction import (
    SUMMARIZED_COUNT_META_KEY,
    SUMMARY_META_KEY,
    compact_conversation,
    should_compact,
)
from .interface import MemoryInterface

if TYPE_CHECKING:
    from agents.state import AgentState, Message

    from .hindsight import HindsightLongTermMemory

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    Facade over short-term and (optionally) long-term memory backends.

    Args:
        short_term: The active short-term memory implementation (rolling window).
        long_term: Reserved for a future message-window long-term backend.
            Currently always None.
        hindsight: Optional Hindsight long-term memory client.  When provided,
            ``recall_long_term()`` and ``retain_turn()`` are activated.
            When None (default), both methods are no-ops — zero behaviour change.
    """

    def __init__(
        self,
        short_term: MemoryInterface,
        long_term: Optional[MemoryInterface] = None,
        hindsight: Optional["HindsightLongTermMemory"] = None,
    ) -> None:
        self.short_term = short_term
        self.long_term = long_term
        self.hindsight = hindsight

    # ------------------------------------------------------------------
    # Short-term context window
    # ------------------------------------------------------------------

    def get_context_window(
        self,
        messages: List["Message"],
        summarized_count: int = 0,
        overhead_tokens: int = 0,
        model: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Return LLM-ready message dicts for the recent conversation tail.

        Messages older than *summarized_count* are represented by the running
        summary (injected separately by the caller); this returns only the
        recent verbatim tail that fits within the memory budget.
        """
        return self.short_term.get_window(messages, summarized_count, overhead_tokens, model)

    # ------------------------------------------------------------------
    # Cross-turn compaction
    # ------------------------------------------------------------------

    async def maybe_compact_conversation(
        self,
        state: "AgentState",
        messages: List["Message"],
        *,
        llm_router: Any,
        model: Optional[str],
        cfg: Optional[Dict[str, Any]] = None,
        progress_emitter: Optional[Any] = None,
    ) -> None:
        """
        Fold older turns into the running summary when the not-yet-summarized
        tail crosses the configured token threshold.

        Reads/writes the summary and watermark on ``state.metadata``. Never
        raises — compaction must not break the turn.

        Args:
            state: The active session state (holds the summary + watermark).
            messages: The conversation history to consider (typically
                ``state.messages`` excluding the current user request).
            llm_router: LLM router used for the summarization call.
            model: Model identifier for the summarization call + budget math.
            cfg: The ``memory.short_term.compaction`` config block.
            progress_emitter: Optional ``ProgressEmitter`` — when the summarization
                LLM call actually runs, a ``compaction_start``/``compaction_end``
                event pair is emitted so the client can show a progress indicator.
        """
        cfg = cfg or {}
        if not bool(cfg.get("enabled", True)):
            return
        if not messages:
            return

        trigger_fraction = float(cfg.get("trigger_fraction", 0.60))
        keep_recent = int(cfg.get("keep_recent_messages", 6))
        max_summary_tokens = int(cfg.get("max_summary_tokens", 1500))

        meta = state.metadata
        watermark = int(meta.get(SUMMARIZED_COUNT_META_KEY, 0) or 0)
        watermark = max(0, min(watermark, len(messages)))

        unsummarized = messages[watermark:]
        if not should_compact(unsummarized, model, trigger_fraction):
            return

        target = len(messages) - keep_recent
        if target <= watermark:
            return  # nothing new to summarize beyond the recent tail

        if progress_emitter is not None:
            await progress_emitter.emit(
                "memory",
                "Compacting conversation history…",
                event_type=SseEventType.COMPACTION_START.value,
            )

        to_summarize = messages[watermark:target]
        prior_summary = str(meta.get(SUMMARY_META_KEY, "") or "")
        new_summary = await compact_conversation(
            prior_summary,
            to_summarize,
            llm_router=llm_router,
            model=model,
            max_summary_tokens=max_summary_tokens,
        )
        compacted = bool(new_summary and new_summary != prior_summary)
        if compacted:
            meta[SUMMARY_META_KEY] = new_summary
            meta[SUMMARIZED_COUNT_META_KEY] = target
            logger.debug(
                "Compacted %d messages into summary (watermark %d→%d) for session %s",
                len(to_summarize),
                watermark,
                target,
                getattr(state, "session_id", "?"),
            )

        if progress_emitter is not None:
            await progress_emitter.emit(
                "memory",
                "This chat has been compacted",
                event_type=SseEventType.COMPACTION_END.value,
                success=compacted,
            )

    # ------------------------------------------------------------------
    # Long-term memory (Hindsight)
    # ------------------------------------------------------------------

    async def recall_long_term(
        self,
        query: str,
        user_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Query Hindsight for facts relevant to *query*.

        Returns a bullet-list string (``"- fact\\n- fact"``), or ``""`` when
        Hindsight is not configured or returns no results.  Never raises.

        Args:
            query: Natural language question (typically the current user message).
            user_id: Identifies which bank to search.
            max_tokens: Override the default max-token cap for recalled facts.
        """
        if self.hindsight is None:
            return ""
        if not user_id:
            logger.debug("[MemoryManager] recall_long_term: skipped — no user_id")
            return ""
        result = await self.hindsight.recall(query, user_id=user_id, max_tokens=max_tokens)
        if result:
            logger.debug(
                "[MemoryManager] recall_long_term: %d chars returned (user=%s)",
                len(result),
                user_id or "default",
            )
        return result

    async def retain_turn(
        self,
        user_message: str,
        assistant_response: str,
        session_id: str,
        user_id: Optional[str] = None,
        dataset_filename: Optional[str] = None,
        turn_index: int = 0,
    ) -> None:
        """
        Persist a completed conversation turn to Hindsight.

        This is always called via ``asyncio.create_task()`` — it must never
        block the response path.  Any error is caught inside
        ``HindsightLongTermMemory.retain()``.

        Args:
            user_message: The user's message for this turn.
            assistant_response: The assistant's response.
            session_id: Current session ID (used as a Hindsight tag).
            user_id: Bank owner identifier.
            dataset_filename: Active dataset filename added as a tag.
            turn_index: Monotone turn counter; combined with session_id it
                forms the ``document_id`` enabling idempotent upserts.
        """
        if self.hindsight is None:
            return
        if not user_id:
            logger.debug("[MemoryManager] retain_turn: skipped — no user_id (unauthenticated)")
            return
        tags: List[str] = []
        if dataset_filename:
            tags.append(f"dataset:{dataset_filename}")
        content = f"User: {user_message}\nAssistant: {assistant_response}"
        document_id = f"{session_id}:turn:{turn_index}"
        await self.hindsight.retain(
            content=content,
            session_id=session_id,
            user_id=user_id,
            document_id=document_id,
            tags=tags,
        )

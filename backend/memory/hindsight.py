"""
HindsightLongTermMemory — async wrapper around the Hindsight local server.

Hindsight (https://hindsight.vectorize.io/) stores extracted facts in a
knowledge graph and retrieves them via multi-strategy semantic search.

We use only retain() and recall():
  - retain(): extract facts from a conversation turn (async, fire-and-forget)
  - recall(): retrieve relevant facts for a query (100–300ms, hot-path safe)

reflect() is intentionally excluded — it runs an internal agentic loop (up
to 10 LLM iterations) which adds unpredictable latency inside our own ReAct
loop.

All network errors are caught and logged as warnings; they never propagate
to the caller so a downed Hindsight server has zero impact on the agent flow.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class HindsightLongTermMemory:
    """
    Thin async wrapper around the Hindsight HTTP client.

    Args:
        base_url: URL of the locally running Hindsight server.
        bank_id_prefix: Namespace prefix for bank IDs.  Per-user banks are
            formed as ``{prefix}:user:{user_id}``.
        recall_budget: Hindsight budget level for recall queries.  ``"low"``
            (100–300 ms) is the right choice for the hot path.
        recall_max_tokens: Maximum token budget for recalled facts injected
            into the orchestrator prompt.  Keep this tight (≤ 2048) to avoid
            bloating the context window.
        retain_extraction_mode: Fact-extraction mode pinned on each bank
            (``"concise"`` | ``"verbose"`` | ``"custom"``).  ``"concise"``
            extracts fewer facts per turn, keeping the knowledge graph small.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8888",
        bank_id_prefix: str = "stych",
        recall_budget: str = "low",
        recall_max_tokens: int = 1024,
        retain_extraction_mode: str = "concise",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bank_id_prefix = bank_id_prefix
        self._recall_budget = recall_budget
        self._recall_max_tokens = recall_max_tokens
        self._retain_extraction_mode = retain_extraction_mode
        self._client: Optional[Any] = None  # lazy-initialised on first use
        # Banks whose extraction mode we've already pinned this process, so we
        # PATCH the config once rather than on every retain.
        self._configured_banks: Set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return (or create) the Hindsight client singleton."""
        if self._client is None:
            try:
                from hindsight_client import Hindsight  # type: ignore[import]

                self._client = Hindsight(base_url=self._base_url)
                logger.info("[HindsightMemory] client initialised → %s", self._base_url)
            except ImportError:
                logger.error(
                    "[HindsightMemory] hindsight-client not installed. "
                    "Run: uv pip install hindsight-client"
                )
                raise
        return self._client

    def _bank_id(self, user_id: Optional[str]) -> str:
        """Form a per-user bank ID, falling back to a shared default."""
        uid = (user_id or "").strip()
        if uid:
            return f"{self._bank_id_prefix}:user:{uid}"
        return f"{self._bank_id_prefix}:default"

    async def _ensure_extraction_mode(self, client: Any, bank: str) -> None:
        """
        Pin ``retain_extraction_mode`` on *bank* once per process.

        Best-effort: bank config is derived from the server default (already
        ``concise``), so a failure here is harmless — we log and move on.  The
        bank is marked attempted regardless of outcome to avoid re-PATCHing on
        every retain.
        """
        if bank in self._configured_banks:
            return
        self._configured_banks.add(bank)
        try:
            from hindsight_client_api.models.bank_config_update import BankConfigUpdate

            await client.banks.update_bank_config(
                bank,
                BankConfigUpdate(updates={"retain_extraction_mode": self._retain_extraction_mode}),
            )
            logger.info(
                "[HindsightMemory] pinned retain_extraction_mode=%s (bank=%s)",
                self._retain_extraction_mode,
                bank,
            )
        except Exception as exc:
            logger.warning(
                "[HindsightMemory] could not pin extraction mode (bank=%s): %s",
                bank,
                exc,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def recall(
        self,
        query: str,
        user_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Retrieve relevant long-term facts for *query* and return them as a
        formatted bullet list, or ``""`` if nothing was found or an error occurred.

        Args:
            query: Natural language question driving the retrieval.
            user_id: Caller-supplied user identifier used to select the bank.
            max_tokens: Override the default ``recall_max_tokens`` cap.
        """
        bank = self._bank_id(user_id)
        try:
            client = self._get_client()
            response = await client.arecall(
                bank_id=bank,
                query=query,
                budget=self._recall_budget,
                max_tokens=max_tokens or self._recall_max_tokens,
                # Include "experience" (raw chunks created immediately on retain) alongside
                # "world" (objective facts) and "observation" (synthesised beliefs).
                # "world"/"observation" are derived by Hindsight's async extraction pipeline
                # and may not exist yet if recall runs shortly after a retain.  Including
                # "experience" ensures we surface freshly retained content immediately.
                types=["world", "observation", "experience"],
            )
            results = getattr(response, "results", None) or []
            if not results:
                logger.info(
                    "[HindsightMemory] recall: 0 results (bank=%s, query=%r)",
                    bank,
                    query[:80],
                )
                return ""
            lines = [f"- {r.text}" for r in results if getattr(r, "text", None)]
            text = "\n".join(lines)
            logger.info(
                "[HindsightMemory] recall: %d fact(s), %d chars (bank=%s)",
                len(lines),
                len(text),
                bank,
            )
            return text
        except Exception as exc:
            logger.warning(
                "[HindsightMemory] recall failed (bank=%s, query=%r): %s",
                bank,
                query[:80],
                exc,
            )
            return ""

    async def retain(
        self,
        content: str,
        session_id: str,
        user_id: Optional[str] = None,
        document_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Persist a conversation turn so Hindsight can extract and index facts.

        This is always called via ``asyncio.create_task()`` — never awaited
        directly in the hot path.  Any exception is swallowed and logged.

        Args:
            content: Raw conversation text (e.g. ``"User: …\\nAssistant: …"``).
            session_id: Current session ID, added as a tag for scoping.
            user_id: Caller-supplied user ID used to select the bank.
            document_id: Stable identifier enabling idempotent upserts.
                Re-retaining the same ID replaces previous memories for that
                document rather than creating duplicates.
            tags: Additional Hindsight tags for future filtering.
            metadata: Arbitrary key-value pairs forwarded to Hindsight.
        """
        bank = self._bank_id(user_id)
        try:
            client = self._get_client()
            await self._ensure_extraction_mode(client, bank)
            _tags: List[str] = list(tags or [])
            if session_id:
                _tags.append(f"session:{session_id}")
            await client.aretain(
                bank_id=bank,
                content=content,
                context="data analysis conversation",
                document_id=document_id,
                tags=_tags,
                metadata=metadata or {},
            )
            logger.info(
                "[HindsightMemory] retain queued doc_id=%s (bank=%s)",
                document_id,
                bank,
            )
        except Exception as exc:
            logger.warning(
                "[HindsightMemory] retain failed (doc_id=%s, bank=%s): %s",
                document_id,
                bank,
                exc,
            )

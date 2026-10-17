"""
Generate a short descriptive title for a chat session using the first N messages.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from llm.infer import LLMRouter

from llm.models import LLMRequest, Message

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 10
_MAX_SNIPPET = 400  # chars per message in the prompt

_SYSTEM_PROMPT = (
    "You are a concise title generator. "
    "Given a conversation excerpt from a data-analysis chat, produce a short title "
    "(4-8 words) that captures the main topic or task. "
    "Respond with ONLY the title — no punctuation at the end, no quotes."
)


async def generate_session_title(
    llm_router: "LLMRouter",
    messages: List[Any],
) -> Optional[str]:
    """
    Summarise the first `_MAX_MESSAGES` user/assistant turns into a short title.

    `messages` may be a list of `Message` model instances or plain dicts with
    ``role`` and ``content`` keys.  Returns the title string or ``None`` on
    any failure (this is best-effort; callers must not rely on it).
    """
    try:
        excerpt_parts: list[str] = []
        for msg in messages[:_MAX_MESSAGES]:
            # Support both model instances and plain dicts
            role: str = getattr(msg, "role", None) or (
                msg.get("role", "") if isinstance(msg, dict) else ""
            )
            content: str = getattr(msg, "content", None) or (
                msg.get("content", "") if isinstance(msg, dict) else ""
            )
            if role not in ("user", "assistant") or not content:
                continue
            label = "User" if role == "user" else "Assistant"
            snippet = content.replace("\n", " ").strip()[:_MAX_SNIPPET]
            excerpt_parts.append(f"{label}: {snippet}")

        if not excerpt_parts:
            return None

        conversation_text = "\n".join(excerpt_parts)
        request = LLMRequest(
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=f"Conversation:\n{conversation_text}\n\nTitle:",
                ),
            ],
            max_tokens=32,
            temperature=0.3,
            metadata={"agent_name": "title_generator"},
        )

        response = await llm_router.generate(request)
        raw = (response.content or "").strip().strip('"').strip("'").strip()
        # Guard against the model returning a long paragraph instead of a title
        title = raw.splitlines()[0].strip() if raw else ""
        return title[:120] or None

    except Exception as exc:
        logger.debug("Session title generation failed: %s", exc)
        return None

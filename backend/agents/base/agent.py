"""
Base agent class for Stych agents using Google ADK.
"""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from google.adk.agents import Agent

from llm.infer import LLMRouter
from llm.models import LLMRequest, Message
from llm.usage_limit import check_usage_limit

logger = logging.getLogger(__name__)


class BaseStychAgent(Agent):
    """Base agent class for Stych agents using Google ADK."""

    # Allow arbitrary fields for Pydantic model
    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(self, name: str, description: str, llm_router: LLMRouter):
        super().__init__(name=name, description=description)
        # Use object.__setattr__ to bypass Pydantic validation for custom fields
        object.__setattr__(self, "llm_router", llm_router)
        object.__setattr__(self, "system_prompt", "")

    async def generate_response_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Generate streaming LLM response using configured router (yields content strings)."""
        check_usage_limit()
        llm_messages = [Message(role=msg["role"], content=msg["content"]) for msg in messages]

        request = LLMRequest(
            messages=llm_messages,
            temperature=temperature,
            stream=True,
            metadata={"agent_name": self.name},
        )

        async for chunk in self.llm_router.generate_stream(request):
            # Providers occasionally yield chunks with None content (tool-call
            # deltas, heartbeat chunks, etc.) — skip them so callers always
            # receive plain str and "".join(...) never raises.
            if chunk.content is not None:
                yield chunk.content

    async def _stream_chunks_with_meta(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
    ):
        """Yield (content, is_thinking) tuples preserving reasoning metadata."""
        llm_messages = [Message(role=msg["role"], content=msg["content"]) for msg in messages]
        request = LLMRequest(
            messages=llm_messages,
            temperature=temperature,
            stream=True,
            metadata={"agent_name": self.name},
        )
        async for chunk in self.llm_router.generate_stream(request):
            if chunk.content is not None:
                is_thinking = bool((chunk.metadata or {}).get("is_thinking", False))
                yield chunk.content, is_thinking

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        """
        Generate LLM response using configured router.

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Temperature for generation (0-2)
            stream: Whether to aggregate a streaming response (default False)

        When a request-scoped ``_active_token_emitter`` is set (injected by the
        SSE route handler), this method automatically streams tokens to the
        frontend so the user sees SQL, Python code, and prose being written
        in real-time.  Reasoning/thinking tokens are emitted on a separate
        ``thinking_token`` SSE channel so the UI can render them in a dedicated
        collapsible section.  The full response string (answer only) is returned.
        """
        check_usage_limit()
        from agents.progress import _active_token_emitter

        # Use the request-scoped emitter when available — bypasses the need to
        # thread the emitter through every call-site explicitly.
        active_emitter = _active_token_emitter.get()

        if stream or active_emitter:
            chunks: List[str] = []
            async for content, is_thinking in self._stream_chunks_with_meta(messages, temperature):
                if not is_thinking:
                    chunks.append(content)
                if active_emitter and content:
                    try:
                        if is_thinking:
                            await active_emitter.emit_thinking_token(content)
                        else:
                            await active_emitter.emit_token(content)
                    except Exception:
                        pass  # never let token emission break the agent
            return "".join(chunks)

        llm_messages = [Message(role=msg["role"], content=msg["content"]) for msg in messages]

        request = LLMRequest(
            messages=llm_messages,
            temperature=temperature,
            metadata={"agent_name": self.name},
        )

        response = await self.llm_router.generate(request)
        return response.content

    async def _llm_decide(
        self,
        prompt: str,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Call the LLM for a structured routing / decision.

        Sends *prompt* as a single user message and parses the response as JSON.
        Uses the router_model (fast/cheap) when provided, otherwise falls back to
        the provider default model.

        Returns the parsed dict on success, or {} on any failure so callers can
        apply their own fallback logic without crashing.
        """
        check_usage_limit()
        messages = [
            Message(
                role="system",
                content=(
                    "You are a decision assistant. "
                    "Respond ONLY with valid JSON — no markdown, no explanation."
                ),
            ),
            Message(role="user", content=prompt),
        ]
        req_kwargs: Dict[str, Any] = dict(
            messages=messages,
            temperature=0.0,
            max_tokens=128,
            metadata={"agent_name": f"{self.name}:decide"},
        )
        if model:
            req_kwargs["model"] = model
        request = LLMRequest(**req_kwargs)
        try:
            response = await self.llm_router.generate(request)
            raw = (response.content or "").strip()
            # Strip markdown fences if the model added them despite instructions
            for fence in ("```json", "```"):
                if raw.startswith(fence):
                    raw = raw[len(fence) :]
            if raw.endswith("```"):
                raw = raw[:-3]
            return json.loads(raw.strip())
        except Exception as exc:
            logger.warning("[%s] _llm_decide failed: %s", self.name, exc)
            return {}

    def _log(self, level: str, msg: str, *args) -> None:
        """Structured log helper prefixed with the agent name."""
        getattr(logger, level)(f"[%s] {msg}", self.name, *args)

"""
Langfuse client for tracking LLM calls and agent interactions.
Targets Langfuse SDK 3.x (langfuse >= 3.0.0).
"""

import contextvars
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-task session ID so concurrent asyncio requests don't overwrite each other.
# Each asyncio Task (one per HTTP request) gets its own independent copy.
_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "langfuse_session_id", default=None
)
_user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "langfuse_user_id", default=None
)

try:
    from langfuse import Langfuse, get_client

    LANGFUSE_AVAILABLE = True
except ImportError:
    LANGFUSE_AVAILABLE = False
    get_client = None
    logger.warning("Langfuse not installed. Tracking will be disabled.")


class LangfuseTracker:
    """
    Langfuse client for tracking LLM calls and agent interactions.
    Uses Langfuse 3.x context manager API.
    """

    def __init__(
        self,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        host: Optional[str] = None,
        enabled: bool = True,
    ):
        self.enabled = enabled and LANGFUSE_AVAILABLE

        if not self.enabled:
            logger.warning("Langfuse tracking disabled")
            return

        # Set env vars so get_client() picks them up automatically
        if public_key:
            os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
        if secret_key:
            os.environ["LANGFUSE_SECRET_KEY"] = secret_key
        if host:
            os.environ["LANGFUSE_HOST"] = host

        try:
            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
            )
            logger.info("Langfuse client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Langfuse: {e}")
            self.enabled = False

    def set_session_id(self, session_id: str) -> None:
        """
        Set the session ID for the *current asyncio task*.
        Uses a ContextVar so concurrent requests never overwrite each other.
        """
        _session_id_var.set(session_id)
        logger.debug(f"[Langfuse] Session ID set: {session_id}")

    def get_session_id(self) -> Optional[str]:
        """Return the session ID for the current asyncio task."""
        return _session_id_var.get()

    def set_user_id(self, user_id: str) -> None:
        """Set authenticated user id for the current asyncio task."""
        _user_id_var.set(user_id)

    def get_user_id(self) -> Optional[str]:
        return _user_id_var.get()

    def log_llm_call(
        self,
        agent_name: str,
        model: str,
        provider: str,
        input_messages: List[Dict[str, Any]],
        output: str,
        latency_ms: float,
        usage: Optional[Dict[str, int]] = None,
        session_id: Optional[str] = None,
    ):
        """
        Log an LLM call to Langfuse.
        session_id arg is kept for backward compat; self._session_id takes priority.
        usage dict keys: prompt_tokens, completion_tokens, total_tokens.
        """
        # Persist billing/consumption events from tokenized calls. This runs
        # regardless of whether Langfuse tracing is enabled — the free-trial spend
        # cap depends on usage being recorded, so it must not be coupled to Langfuse.
        effective_user_id = _user_id_var.get()
        if usage and effective_user_id:
            try:
                from .usage_tracker import get_usage_tracker

                get_usage_tracker().record_event(
                    user_id=effective_user_id,
                    session_id=_session_id_var.get() or session_id,
                    provider=provider,
                    model=model,
                    agent_name=agent_name,
                    usage=usage,
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                logger.error("[UsageTracker] Failed to record usage: %s", exc)

        if not self.enabled:
            logger.debug("Langfuse tracking disabled - skipping LLM call logging")
            return

        effective_session_id = _session_id_var.get() or session_id
        token_count = usage.get("total_tokens", "N/A") if usage else "N/A"

        logger.info(
            f"📊 [Langfuse] LLM Call: {agent_name} -> {provider}/{model} "
            f"(session: {effective_session_id}, latency: {latency_ms}ms, tokens: {token_count})"
        )

        try:
            client = get_client()

            usage_details = None
            if usage:
                usage_details = {
                    "input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                    "total": usage.get("total_tokens", 0),
                }

            with client.start_as_current_generation(
                name=f"{agent_name}/{provider}/{model}",
                model=model,
                input=input_messages,
                output=output,
                usage_details=usage_details,
                metadata={
                    "provider": provider,
                    "agent_name": agent_name,
                    "latency_ms": latency_ms,
                    "session_id": effective_session_id,
                    "user_id": effective_user_id,
                },
            ):
                client.update_current_trace(session_id=effective_session_id)

            logger.info(f"✅ [Langfuse] Logged LLM call: {provider}/{model}")
        except Exception as e:
            logger.error(f"[Langfuse] Failed to log LLM call: {e}")

    def log_agent_execution(
        self,
        agent_name: str,
        task: str,
        input_data: Any,
        output_data: Any,
        execution_time: float,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Log an agent execution to Langfuse.
        session_id arg is kept for backward compat; self._session_id takes priority.
        """
        if not self.enabled:
            return

        effective_session_id = _session_id_var.get() or session_id

        logger.info(
            f"🤖 [Langfuse] Agent: {agent_name}, Task: {task}, "
            f"Time: {execution_time}s, Session: {effective_session_id}"
        )

        try:
            client = get_client()

            with client.start_as_current_span(
                name=f"{agent_name}_execution",
                input=input_data,
                output=output_data,
                metadata={
                    "agent_name": agent_name,
                    "task": task,
                    "execution_time_seconds": execution_time,
                    "session_id": effective_session_id,
                    **(metadata or {}),
                },
            ):
                client.update_current_trace(session_id=effective_session_id)

            logger.info(f"✅ [Langfuse] Logged agent execution: {agent_name}")
        except Exception as e:
            logger.error(f"[Langfuse] Failed to log agent execution: {e}")

    def flush(self):
        """Flush any pending events to Langfuse."""
        if not self.enabled:
            return
        try:
            get_client().flush()
            logger.debug("✅ [Langfuse] Flushed events")
        except Exception as e:
            logger.error(f"[Langfuse] Flush failed: {e}")


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_tracker: Optional[LangfuseTracker] = None


def get_langfuse_tracker() -> Optional[LangfuseTracker]:
    """Get the global Langfuse tracker instance."""
    return _tracker


def init_langfuse_tracker(config: Dict[str, Any]) -> None:
    """Initialize the global Langfuse tracker from config dict."""
    global _tracker

    langfuse_config = config.get("langfuse", {})

    if not langfuse_config.get("enabled", False):
        # The tracker also persists token/cost usage. Keep a disabled instance
        # so billing remains active even when external Langfuse tracing is off.
        logger.info("Langfuse tracing disabled in config; usage tracking remains active")
        _tracker = LangfuseTracker(enabled=False)
        return

    _tracker = LangfuseTracker(
        public_key=langfuse_config.get("public_key"),
        secret_key=langfuse_config.get("secret_key"),
        host=langfuse_config.get("host"),
        enabled=True,
    )
    logger.info("Global Langfuse tracker initialized")

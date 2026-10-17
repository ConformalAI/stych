"""
Usage limit — permanent, account-wide USD spend cap.

A permanent, account-wide USD spend cap for users without an exempt email.
Lifetime spend is SUM(LLMUsageEvent.estimated_cost_usd) per user (see
UsageTracker.get_lifetime_spend_usd). Enforcement is request-scoped: the route
handler stamps a context (enabled / limit / user_id / exempt) at the start of a
request, and LLMRouter.generate[_stream] calls check_usage_limit() before
every provider call. Because every agent (orchestrator + sub-agents) goes through
the router, this is the single uniform detection point.

UsageLimitReached inherits from BaseException on purpose: sub-agents and the
react engine wrap tool execution in `except Exception`, so a plain Exception would
be swallowed and converted to an error string. Deriving from BaseException lets the
signal propagate cleanly up to the route handler (mirrors PauseSignal handling).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class UsageLimitReached(BaseException):
    """Raised when a user's lifetime spend has reached the usage cap."""

    def __init__(self, spend: float, limit: float) -> None:
        super().__init__(f"Usage limit reached: ${spend:.4f} >= ${limit:.2f}")
        self.spend = spend
        self.limit = limit


# Per-request enforcement context: {enabled, limit_usd, user_id, exempt}.
# None when no request is active. Same ContextVar pattern as _active_token_emitter.
_usage_limit_ctx: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "_usage_limit_ctx", default=None
)


def LIMIT_REACHED_MESSAGE(limit: float) -> str:
    """User-facing notice shown when the cap is hit (no upgrade wording)."""
    return (
        f"You've reached your usage limit of ${limit:.2f}. "
        "Please contact the team to continue using Stych."
    )


def _coerce_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def set_usage_limit_context(config: Dict[str, Any], user_id: Optional[str], email: Optional[str]):
    """
    Stamp the per-request usage-limit enforcement context. Returns a reset token.

    `enabled` and `limit_usd` come from config (env substitution yields strings, so
    they are coerced). A user whose email ends with the exempt domain is never
    limited.
    """
    ul = config.get("usage_limit", {}) if isinstance(config, dict) else {}
    enabled = _coerce_bool(ul.get("enabled", "false"))
    limit_usd = _coerce_float(ul.get("limit_usd", 1.0), 1.0)
    domain = str(ul.get("exempt_email_domain", "") or "").strip().lower()

    exempt = bool(email) and bool(domain) and email.strip().lower().endswith("@" + domain)

    return _usage_limit_ctx.set(
        {
            "enabled": enabled,
            "limit_usd": limit_usd,
            "user_id": user_id,
            "exempt": exempt,
        }
    )


def reset_usage_limit_context(token) -> None:
    try:
        _usage_limit_ctx.reset(token)
    except (ValueError, LookupError):
        _usage_limit_ctx.set(None)


def _active_ctx() -> Optional[Dict[str, Any]]:
    """Return the context only when enforcement applies to this request."""
    ctx = _usage_limit_ctx.get()
    if not ctx or not ctx.get("enabled") or ctx.get("exempt"):
        return None
    if not ctx.get("user_id"):
        return None
    return ctx


def over_limit_message_or_none() -> Optional[str]:
    """
    Return the user-facing limit notice if the current request's user is already
    at/over the cap, else None. Used by the route to block new messages up front.
    """
    ctx = _active_ctx()
    if ctx is None:
        return None
    from llm.usage_tracker import get_usage_tracker

    spend = get_usage_tracker().get_lifetime_spend_usd(user_id=ctx["user_id"])
    if spend >= ctx["limit_usd"]:
        return LIMIT_REACHED_MESSAGE(ctx["limit_usd"])
    return None


def check_usage_limit() -> None:
    """
    Raise UsageLimitReached if the current user is at/over the cap.

    No-op when no request context is set, the feature is disabled, the user is
    exempt, or spend is under the limit. Called before every LLM provider call.
    """
    ctx = _active_ctx()
    if ctx is None:
        return
    from llm.usage_tracker import get_usage_tracker

    spend = get_usage_tracker().get_lifetime_spend_usd(user_id=ctx["user_id"])
    limit = ctx["limit_usd"]
    if spend >= limit:
        raise UsageLimitReached(spend, limit)

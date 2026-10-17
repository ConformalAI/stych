"""Tests for the usage limit spend cap (llm/usage_limit.py + UsageTracker)."""

from __future__ import annotations

import uuid

import pytest

from database.models import LLMUsageEvent
from llm.usage_limit import (
    UsageLimitReached,
    check_usage_limit,
    over_limit_message_or_none,
    reset_usage_limit_context,
    set_usage_limit_context,
)
from llm.usage_tracker import get_usage_tracker

CONFIG = {
    "usage_limit": {
        "enabled": "true",
        "limit_usd": "1.0",
        "exempt_email_domain": "exempt.example.org",
    }
}


def _add_spend(db, user_id: str, cost_usd: float) -> None:
    db.add(
        LLMUsageEvent(
            user_id=user_id,
            provider="litellm",
            model="claude-sonnet-4.5",
            agent_name="orchestrator",
            estimated_cost_usd=cost_usd,
        )
    )
    db.commit()


def test_get_lifetime_spend_usd_sums_events(db_session):
    user_id = f"u-{uuid.uuid4()}"
    _add_spend(db_session, user_id, 0.30)
    _add_spend(db_session, user_id, 0.45)

    spend = get_usage_tracker().get_lifetime_spend_usd(user_id=user_id)
    assert spend == pytest.approx(0.75)


def test_get_lifetime_spend_usd_zero_for_unknown_user():
    spend = get_usage_tracker().get_lifetime_spend_usd(user_id=f"u-{uuid.uuid4()}")
    assert spend == 0.0


def test_under_limit_is_allowed(db_session):
    user_id = f"u-{uuid.uuid4()}"
    _add_spend(db_session, user_id, 0.50)

    token = set_usage_limit_context(CONFIG, user_id, "user@example.com")
    try:
        assert over_limit_message_or_none() is None
        check_usage_limit()  # must not raise
    finally:
        reset_usage_limit_context(token)


def test_over_limit_raises_and_messages(db_session):
    user_id = f"u-{uuid.uuid4()}"
    _add_spend(db_session, user_id, 1.25)

    token = set_usage_limit_context(CONFIG, user_id, "user@example.com")
    try:
        msg = over_limit_message_or_none()
        assert msg is not None and "usage limit" in msg.lower()
        with pytest.raises(UsageLimitReached):
            check_usage_limit()
    finally:
        reset_usage_limit_context(token)


def test_exempt_email_never_limited(db_session):
    user_id = f"u-{uuid.uuid4()}"
    _add_spend(db_session, user_id, 5.00)  # way over

    token = set_usage_limit_context(CONFIG, user_id, "person@exempt.example.org")
    try:
        assert over_limit_message_or_none() is None
        check_usage_limit()  # must not raise
    finally:
        reset_usage_limit_context(token)


def test_disabled_feature_never_limited(db_session):
    user_id = f"u-{uuid.uuid4()}"
    _add_spend(db_session, user_id, 5.00)

    disabled = {"usage_limit": {**CONFIG["usage_limit"], "enabled": "false"}}
    token = set_usage_limit_context(disabled, user_id, "user@example.com")
    try:
        assert over_limit_message_or_none() is None
        check_usage_limit()  # must not raise
    finally:
        reset_usage_limit_context(token)


def test_no_context_is_noop():
    # No context set for this task → enforcement is inert.
    assert over_limit_message_or_none() is None
    check_usage_limit()


async def test_mid_flight_limit_stops_second_llm_call(db_session):
    """
    Simulate a request that starts just under the limit, records spend after the
    first LLM call that pushes the user over, then verify that the agent-layer
    check_usage_limit() raises UsageLimitReached on the second call — before the
    provider is reached.

    The check lives at the agent layer (BaseStychAgent, react engine, orchestrator
    react, and individual agent direct call sites), not inside LLMRouter. This test
    mirrors exactly what each of those call sites does: call check_usage_limit()
    immediately before calling router.generate().
    """
    from llm.infer import LLMRouter
    from llm.models import LLMRequest, LLMResponse, Message

    user_id = f"u-{uuid.uuid4()}"
    limit_usd = 1.0

    # User starts $0.10 under the limit.
    _add_spend(db_session, user_id, 0.90)

    config = {
        "usage_limit": {
            "enabled": "true",
            "limit_usd": str(limit_usd),
            "exempt_email_domain": "exempt.example.org",
        },
        "provider": "litellm",
        "litellm": {"base_url": "http://unused", "default_model": "claude-sonnet-4.5"},
    }

    token = set_usage_limit_context(config, user_id, "user@example.com")
    try:
        fake_response = LLMResponse(
            content="hello",
            model="claude-sonnet-4.5",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            finish_reason="stop",
            metadata={"provider": "litellm"},
        )

        router = LLMRouter(config)
        provider_call_count = 0

        async def fake_provider_generate(req):
            nonlocal provider_call_count
            provider_call_count += 1
            return fake_response

        router.provider.generate = fake_provider_generate

        req = LLMRequest(
            messages=[Message(role="user", content="hello")],
            max_tokens=10,
        )

        # First call: agent checks limit (passes), then calls router.
        check_usage_limit()
        result = await router.generate(req)
        assert result.content == "hello"
        assert provider_call_count == 1

        # Simulate the spend that the first call would have recorded pushing user over.
        _add_spend(db_session, user_id, 0.20)

        # Second call: agent-layer check fires before router is reached.
        with pytest.raises(UsageLimitReached):
            check_usage_limit()

        # Provider was NOT called a second time.
        assert provider_call_count == 1
    finally:
        reset_usage_limit_context(token)

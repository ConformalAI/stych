"""Tests for per-user long-term memory preference helpers."""

from agents.state import AgentState, long_term_memory_allowed


def test_long_term_memory_allowed_by_default():
    state = AgentState(session_id="s1", metadata={"user_id": "u1"})
    assert long_term_memory_allowed(state) is True


def test_long_term_memory_blocked_for_anonymous():
    state = AgentState(
        session_id="s1",
        metadata={"user_id": "u1", "is_anonymous": True},
    )
    assert long_term_memory_allowed(state) is False


def test_long_term_memory_blocked_when_user_opted_out():
    state = AgentState(
        session_id="s1",
        metadata={"user_id": "u1", "long_term_memory_enabled": False},
    )
    assert long_term_memory_allowed(state) is False

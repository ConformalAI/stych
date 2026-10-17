"""Linking HITL: propose a join plan → pause → structured confirm/decline.

Replaces the old free-text confirmation regex ("yes", "merge", …) that fired
saved plans off unrelated messages.
"""

from __future__ import annotations

import types

import pandas as pd
import pytest

from agents.linking.execution import plan_physical_join
from agents.orchestrator.subagent_calls import LINKING_PLAN_KEY, subagent_linking

_PLAN = [
    {
        "left": "a.csv",
        "right": "b.csv",
        "left_on": "id",
        "right_on": "key",
        "strategy": "exact",
        "join_type": "LEFT",
    }
]


class _FakeState:
    def __init__(self):
        self.metadata = {}
        self.data_context = types.SimpleNamespace(dataframe=None, file_id="orig")
        # _register_joined_dataset promotes the join to a tracked DataContext,
        # preserving the previous active dataset as an additional context.
        self.additional_data_contexts = []
        self._datasets = [
            {"name": "a.csv", "data": pd.DataFrame({"id": [1, 2], "v": ["x", "y"]})},
            {"name": "b.csv", "data": pd.DataFrame({"key": [1, 2], "w": [10, 20]})},
        ]

    def get_all_datasets_for_linking(self):
        return self._datasets

    def get_all_data_contexts(self):
        """File-backed datasets: no source identity, so joins stay materialized."""
        return [
            types.SimpleNamespace(filename=d["name"], dataframe=d["data"], metadata={})
            for d in self._datasets
        ]

    def set_data_context(self, dc):
        self.data_context = dc


def _fake_orch(join_result=None):
    orch = types.SimpleNamespace()
    orch.decide_prompts = []

    async def _llm_decide(prompt):
        orch.decide_prompts.append(prompt)
        return {"join_plan": _PLAN, "reasoning": "ids match"}

    async def multi_join(**kwargs):
        return join_result or {
            "data": [{"id": 1, "v": "x", "w": 10}],
            "rows": 1,
            "columns": ["id", "v", "w"],
            "datasets_count": 2,
            "steps_executed": 1,
            "step_log": [],
        }

    async def format_data(payload, question="", context=None):
        return f"joined: {payload['result_shape']['rows']} rows"

    orch._llm_decide = _llm_decide
    orch.linking_agent = types.SimpleNamespace(multi_join=multi_join)
    orch.formatter_agent = types.SimpleNamespace(format_data=format_data)
    return orch


@pytest.mark.asyncio
async def test_fresh_run_proposes_and_pauses():
    state = _FakeState()
    orch = _fake_orch()
    out = await subagent_linking(orch, "join my files", state, "join my files", None, None)
    assert out["status"] == "awaiting_user_action"
    assert "Omit threshold entirely when strategy is exact" in orch.decide_prompts[0]
    prompt = out["action_prompt"]
    assert prompt["kind"] == "linking_confirm"
    assert {o["value"] for o in prompt["options"]} == {"accept", "decline"}
    # The plan is parked for the resume turn — nothing executed yet.
    assert state.metadata[LINKING_PLAN_KEY]["join_plan"] == _PLAN
    assert "last_linking_result" not in state.metadata
    # Propose turn surfaces its reasoning trace (inspect → propose).
    trace = out["metadata"]["reasoning_trace"]
    assert [s["action"] for s in trace] == ["inspect_datasets", "propose_plan"]


@pytest.mark.asyncio
async def test_accept_executes_saved_plan(monkeypatch):
    state = _FakeState()
    state.metadata[LINKING_PLAN_KEY] = {
        "join_plan": _PLAN,
        "reasoning": "ids match",
        "physical_plan": plan_physical_join(state.get_all_data_contexts(), _PLAN, {}),
    }
    monkeypatch.setattr(
        "execution.joins.execute_exact_snapshot_join",
        lambda *_args, **_kwargs: {
            "data": [{"id": 1, "v": "x", "w": 10}],
            "rows": 1,
            "columns": ["id", "v", "w"],
            "datasets_count": 2,
            "steps_executed": 1,
            "step_log": [{"engine": "duckdb"}],
        },
    )
    out = await subagent_linking(
        _fake_orch(),
        "",
        state,
        "",
        None,
        None,
        user_action={"kind": "linking_confirm", "value": "accept", "prompt_id": "p1"},
    )
    assert out["status"] == "completed"
    assert "joined: 1 rows" in out["response"]
    assert state.metadata["last_linking_result"]["rows"] == 1
    assert LINKING_PLAN_KEY not in state.metadata  # consumed
    # Execute turn surfaces its own trace segment (execute start → complete).
    assert [s["action"] for s in out["metadata"]["reasoning_trace"]] == [
        "execute_join",
        "execute_join",
    ]


@pytest.mark.asyncio
async def test_decline_cancels_without_executing():
    state = _FakeState()
    state.metadata[LINKING_PLAN_KEY] = {"join_plan": _PLAN, "reasoning": "ids match"}
    out = await subagent_linking(
        _fake_orch(),
        "",
        state,
        "",
        None,
        None,
        user_action={"kind": "linking_confirm", "value": "decline", "prompt_id": "p1"},
    )
    assert out["status"] == "completed"
    assert "did not execute" in out["response"]
    assert "last_linking_result" not in state.metadata
    assert LINKING_PLAN_KEY not in state.metadata  # stale plan can never re-fire


@pytest.mark.asyncio
async def test_confirm_without_saved_plan_asks_again():
    state = _FakeState()
    out = await subagent_linking(
        _fake_orch(),
        "",
        state,
        "",
        None,
        None,
        user_action={"kind": "linking_confirm", "value": "accept", "prompt_id": "p1"},
    )
    assert out["status"] == "completed"
    assert "no longer available" in out["response"]


@pytest.mark.asyncio
async def test_stale_join_prompt_id_cannot_consume_latest_plan():
    state = _FakeState()
    orch = _fake_orch()
    proposed = await subagent_linking(orch, "join my files", state, "join my files", None, None)
    latest = proposed["action_prompt"]

    out = await subagent_linking(
        orch,
        "",
        state,
        "",
        None,
        None,
        user_action={
            "kind": "linking_confirm",
            "value": "accept",
            "prompt_id": "stale-prompt",
        },
    )

    assert out["status"] == "awaiting_user_action"
    assert out["action_prompt"]["prompt_id"] == latest["prompt_id"]
    assert LINKING_PLAN_KEY in state.metadata
    assert "last_linking_result" not in state.metadata

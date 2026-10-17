"""Contract tests for the declarative agent registry.

The registry (helpers/agent_registry.py) is the single source of truth for the
orchestrator's roster: the call_agent tool enum, the LLM prompt guide, dispatch,
the unknown-agent fallback, and the API task_type pattern are all derived from
it. These tests pin that derivation.
"""

from __future__ import annotations

import re

import pytest

from helpers import agent_registry as reg
from helpers.agent_registry import AGENTS, LLM_EXPOSED


def _wrapper_names() -> set[str]:
    from agents.orchestrator import subagent_calls

    return {n for n in dir(subagent_calls) if n.startswith("subagent_")}


def test_every_wrapper_exists_in_subagent_calls():
    wrappers = _wrapper_names()
    for spec in AGENTS:
        assert spec.wrapper in wrappers, f"{spec.id}: {spec.wrapper} missing"


def test_unique_ids_and_aliases():
    names = [spec.id for spec in AGENTS] + [alias for spec in AGENTS for alias in spec.aliases]
    assert len(names) == len(set(names)), "duplicate agent id/alias in registry"


def test_lookup_resolves_aliases_case_insensitive():
    assert reg.lookup("graph").id == "graph"
    assert reg.lookup("CHART").id == "graph"
    assert reg.lookup(" knowledge ").id == "domain_qa"
    assert reg.lookup("explore").id == "general_analysis"
    assert reg.lookup("nope") is None


def test_llm_enum_matches_exposed_specs():
    assert reg.llm_enum() == [spec.id for spec in LLM_EXPOSED]
    # Hidden agents must not leak into the LLM-facing enum.
    assert "general_analysis" not in reg.llm_enum()


def test_short_circuit_task_types_consistent():
    for spec in AGENTS:
        if spec.short_circuit_task_type is None:
            continue
        assert spec.short_circuit_task_type == spec.id or spec.short_circuit_task_type in (
            spec.id,
            *spec.aliases,
        ), f"{spec.id}: short-circuit task type doesn't match the agent"
    assert "filter" in reg.short_circuit_task_types()


def test_api_task_types_is_sorted_superset():
    types = reg.api_task_types()
    assert types == sorted(types)
    for tt in reg.BASE_TASK_TYPES + tuple(reg.short_circuit_task_types()):
        assert tt in types


def test_schemas_pattern_derived_from_registry():
    from api.schemas import QueryRequest

    field = QueryRequest.model_fields["task_type"]
    pattern = field.metadata[0].pattern
    for tt in reg.api_task_types():
        assert re.fullmatch(pattern, tt), f"registry task type {tt!r} rejected by schema"
    with pytest.raises(ValueError):
        QueryRequest(task_type="bogus", message="x", session_id="s")


async def test_dispatch_unknown_agent_lists_all_ids():
    from agents.orchestrator.agent import OrchestratorAgent

    result = await OrchestratorAgent._call_agent_dispatch(
        object(), "bogus_agent", "p", "m", None, None, None, None
    )
    assert result["status"] == "completed"
    for agent_id in reg.all_dispatchable_ids():
        assert agent_id in result["response"]


def test_tool_schema_enum_derived():
    from agents.orchestrator.orchestrator_react import ORCHESTRATOR_TOOLS

    call_agent = next(t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "call_agent")
    assert call_agent["function"]["parameters"]["properties"]["agent"]["enum"] == (reg.llm_enum())


def test_system_prompt_guide_matches_registry_descriptions():
    from agents.orchestrator.orchestrator_react import _build_system_prompt

    prompt = _build_system_prompt("auto")
    for spec in LLM_EXPOSED:
        line_prefix = f"  {spec.id:<15}— "
        assert (
            line_prefix + spec.description.split("\n")[0] in prompt
        ), f"prompt guide out of sync for {spec.id}"


def test_system_prompt_routes_scoped_writes_and_joins_to_the_owning_agents():
    from agents.orchestrator.orchestrator_react import _build_system_prompt

    prompt = _build_system_prompt("auto")
    assert "cannot modify source tables, create arbitrary database objects" in prompt
    assert "the only agent for joining / merging / matching multiple attached datasets" in prompt

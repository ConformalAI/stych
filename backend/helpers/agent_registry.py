"""Declarative sub-agent registry.

Single source of truth for the orchestrator's agent roster. Adding an agent
means adding ONE AgentSpec entry here; the call_agent tool enum, the LLM
system-prompt guide, the dispatch chain, the unknown-agent fallback text, and
the task_type validation pattern are all derived from these definitions
(backend-side directly, frontend-side via scripts/export_contract_schemas.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agents.state import AgentState

    SubagentFn = Callable[
        [
            Any,  # OrchestratorAgent
            str,  # subtask_prompt
            "AgentState",
            str,  # user_message
            "str | None",  # session_id
            Any,  # progress_emitter
        ],
        Any,
    ]


@dataclass(frozen=True)
class AgentSpec:
    """One dispatchable sub-agent.

    id          — canonical name used by the LLM tool enum and dispatch.
    aliases     — alternate names accepted by dispatch (never exposed to the LLM).
    description — verbatim prose for the system-prompt agent guide.
    wrapper     — attribute name on agents.orchestrator.subagent_calls.
    takes_user_action — wrapper accepts a ``user_action`` kwarg.
    takes_domain      — wrapper accepts a ``domain_label`` kwarg.
    llm_exposed — listed in the call_agent tool enum and prompt guide when True;
                  hidden-but-dispatchable (internal/legacy routes) when False.
    short_circuit_task_type — task_type that bypasses the ReAct loop straight to
                  this agent (None = reached only through orchestration).
    """

    id: str
    description: str
    wrapper: str
    aliases: tuple[str, ...] = field(default=())
    takes_user_action: bool = False
    takes_domain: bool = False
    llm_exposed: bool = True
    short_circuit_task_type: str | None = None


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="analysis",
        description=(
            "statistics, Python code execution, trend detection, correlation. "
            "Can write workspace artifacts for file sessions and mutate only the active private "
            "SQL working copy; it cannot modify source tables, create arbitrary database objects, "
            "or join another attached dataset."
        ),
        wrapper="subagent_analysis",
        takes_user_action=True,
    ),
    AgentSpec(
        id="cleaning",
        description=(
            "data quality, deduplication, type fixing, null handling. "
            "Cleaning and analysis can modify workspace files or Stych-owned private SQL working "
            "copies; they never write the source table. Cleaning also manages named working copies "
            "and is the only agent that can persist an approved database copy."
        ),
        wrapper="subagent_cleaning",
        takes_user_action=True,
    ),
    AgentSpec(
        id="graph",
        aliases=("visualization", "chart"),
        description=(
            "creates and EDITS persistent visualizations (they appear in the "
            "user's Visualize tab and stay live as data changes). Call this for any chart request, "
            "AND for edit requests about an existing chart ('make it monthly', 'stack it', 'change "
            "to a pie') — pass the edit intent through; the agent finds and updates the chart. "
            "analysis does NOT produce charts."
        ),
        wrapper="subagent_graph",
    ),
    AgentSpec(
        id="linking",
        description=(
            "the only agent for joining / merging / matching multiple attached datasets, including "
            "tables on one connected database or warehouse. "
            "It proposes a join plan and pauses for the user's structured confirmation "
            "before creating a joined working artifact; you do not need to re-dispatch it on "
            "confirmation."
        ),
        wrapper="subagent_linking",
        takes_user_action=True,
    ),
    AgentSpec(
        id="qa",
        description="factual Q&A about the current dataset",
        wrapper="subagent_qa",
    ),
    AgentSpec(
        id="domain_qa",
        aliases=("knowledge",),
        description=("domain concepts (stats/SQL/ML) with no dataset"),
        wrapper="subagent_domain_qa",
        takes_domain=True,
    ),
    AgentSpec(
        id="filter",
        description=(
            "create or edit SAVED preview filters: persistent highlight rules "
            "shown in the Data tab (flag/highlight/mark rows or cells matching criteria, find fuzzy "
            "or exact duplicate rows visually, conditional formatting). Filters never modify data — "
            "for actual dedup/fixes use cleaning instead."
        ),
        wrapper="subagent_filter",
        short_circuit_task_type="filter",
    ),
    AgentSpec(
        id="integration",
        description=(
            "third-party services reached over an API: cloud storage and "
            "document tools, MCP-enabled tools, and public/open dataset providers. Call it to "
            "list, browse, upload, or manage files with those services, or to fetch external data. "
            "The integration agent discovers available connections at runtime.\n"
            "    NOT for connected data sources. A warehouse or database the user has already "
            "attached (BigQuery, Databricks, Snowflake, Redshift, PostgreSQL, …) appears in the "
            "workspace catalog below as a `sql` source and is queried with the analysis, sql, "
            "cleaning, graph, or filter agents. Routing one of those to integration will fail: "
            "the data is already connected, and the integration agent cannot see it."
        ),
        wrapper="subagent_integration",
    ),
    # Hidden from the LLM tool enum and guide; kept dispatchable for internal
    # callers and legacy alias routing.
    AgentSpec(
        id="general_analysis",
        aliases=("explore",),
        description=(
            "open-ended multi-step exploration combining analysis, qa, and graph "
            "capabilities for broad investigative requests"
        ),
        wrapper="subagent_general_analysis",
        llm_exposed=False,
    ),
)

_BY_NAME: dict[str, AgentSpec] = {
    name.lower(): spec for spec in AGENTS for name in (spec.id, *spec.aliases)
}

LLM_EXPOSED: tuple[AgentSpec, ...] = tuple(a for a in AGENTS if a.llm_exposed)


def lookup(name: str) -> AgentSpec | None:
    """Resolve an id or alias (case-insensitive) to its spec."""
    return _BY_NAME.get((name or "").lower().strip())


def llm_enum() -> list[str]:
    """Agent ids offered to the LLM in the call_agent tool schema."""
    return [a.id for a in LLM_EXPOSED]


def all_dispatchable_ids() -> list[str]:
    """Every canonical id (including hidden ones), registry order."""
    return [a.id for a in AGENTS]


def short_circuit_task_types() -> list[str]:
    """task_types that short-circuit straight to an agent."""
    return [a.short_circuit_task_type for a in AGENTS if a.short_circuit_task_type]


# Orchestrator-level modes that are not agents. `ontology` is the /ontology
# slash command: a routing hint that steers the ReAct loop at the semantic
# layer's tools; there is no ontology sub-agent to short-circuit to.
BASE_TASK_TYPES = ("auto", "eda", "ontology")
# Agent ids accepted as soft routing hints (bias the orchestrator) without
# short-circuiting the ReAct loop.
HINTABLE_AGENT_IDS = ("analysis", "cleaning", "qa", "linking")


def api_task_types() -> list[str]:
    """Every task_type value accepted on the wire (QueryRequest.task_type)."""
    return sorted({*BASE_TASK_TYPES, *HINTABLE_AGENT_IDS, *short_circuit_task_types()})

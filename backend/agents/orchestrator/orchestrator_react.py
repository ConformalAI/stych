"""
ReAct control loop for the orchestrator: task decomposition and call_agent.

The orchestrator has no final_answer tool — when done, it outputs plain text
(no tool call) and the loop detects that as the terminal state.
Sub-agents own all user-facing HITL; the orchestrator has no `question` tool.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from llm.models import Message
from memory.compaction import SUMMARIZED_COUNT_META_KEY, SUMMARY_META_KEY
from react_engine.loop import (
    PauseSignal,
    output_cap_from_config,
    run_agent_loop,
    transcript_budget_from_config,
)
from react_engine.schema import ReactStepRecord, preview
from react_engine.toolkit import Toolkit

if TYPE_CHECKING:  # pragma: no cover
    from agents.orchestrator.agent import OrchestratorAgent
    from agents.progress import ProgressEmitter
    from agents.state import AgentState

from agents.state import long_term_memory_allowed
from helpers.agent_registry import llm_enum
from tools.ontology_tools import (
    ONTOLOGY_GUIDE,
    ONTOLOGY_TOOL_SCHEMAS,
    OntologyToolRegistry,
)
from tracing.trace_parts import orch_part_id, step_part

logger = logging.getLogger(__name__)

# Unified-engine pending record (carries the native transcript + in-flight sub-agent).
ORCH_ENGINE_PENDING_KEY = "orchestrator_engine_pending"
MAX_OBS = 3000
DEFAULT_REACT_MAX_STEPS = 20

# ---------------------------------------------------------------------------
# Native tool schemas — passed to the LLM so it emits structured tool_use
# blocks instead of JSON text.
# ---------------------------------------------------------------------------
ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "call_agent",
            "description": "Delegate a subtask to a specialized sub-agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        # Derived from the declarative agent registry.
                        "enum": llm_enum(),
                        "description": (
                            "Which agent to call. Use 'integration' only for third-party "
                            "services reached over an API: fetching public/external datasets, "
                            "pushing results to Google Sheets, or calling an MCP-connected "
                            "tool. Do NOT use it for a database or warehouse the user has "
                            "already connected (BigQuery, Databricks, Snowflake, Redshift, "
                            "PostgreSQL) — those are `sql` sources in the workspace catalog "
                            "and belong to analysis/sql/cleaning/graph/filter."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Specific, self-contained task for this agent.",
                    },
                    "input": {
                        "type": "string",
                        "description": (
                            "Optional. The id of the workspace source the agent should "
                            "work on (a file path or sql: handle from the sources catalog). "
                            "Use this to direct an agent at an agent-created file rather "
                            "than the default active dataset."
                        ),
                    },
                    "domain": {
                        "type": "string",
                        "description": "Optional domain context (used by domain_qa).",
                    },
                },
                "required": ["agent", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "Search the user's long-term memory for facts from past sessions. "
                "Use when the user references prior work or historical context would help."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for long-term memory recall.",
                    }
                },
                "required": ["query"],
            },
        },
    },
] + ONTOLOGY_TOOL_SCHEMAS

# Anonymous-session variant: recall_memory removed so it is never offered to the LLM.
DESCRIBE_WORKSPACE_TOOL = {
    "type": "function",
    "function": {
        "name": "describe_workspace",
        "description": (
            "Inspect what this session actually contains before routing work: every "
            "connected data source with the system it came from, which of them are "
            "the same connection, what each connector supports (analysis, cleaning, "
            "filtering, diff, export, joins) and where it is limited, plus the "
            "workspace files. Call this when a request names a warehouse or dataset, "
            "when more than one source is attached and you must pick the right one, "
            "or before telling a user something is unsupported."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

ORCHESTRATOR_TOOLS.append(DESCRIBE_WORKSPACE_TOOL)

ORCHESTRATOR_TOOLS_ANON = [
    t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] != "recall_memory"
]

_ONTOLOGY_TOOL_NAMES = frozenset(t["function"]["name"] for t in ONTOLOGY_TOOL_SCHEMAS)


def _orch_config(orch: "OrchestratorAgent") -> Dict[str, Any]:
    r = getattr(orch, "_config", None) or {}
    merged: Dict[str, Any] = r.get("orchestrate_react", {})
    if not merged and r.get("routing"):
        merged = (r.get("routing") or {}).get("orchestrate_react", {}) or {}
    return merged if isinstance(merged, dict) else {}


def _max_steps(orch: "OrchestratorAgent") -> int:
    return int(_orch_config(orch).get("react_max_steps", DEFAULT_REACT_MAX_STEPS))


def _short_term_cfg(orch: "OrchestratorAgent") -> Dict[str, Any]:
    r = getattr(orch, "_config", None) or {}
    st = (r.get("memory", {}) or {}).get("short_term", {}) or {}
    return st if isinstance(st, dict) else {}


def _compaction_cfg(orch: "OrchestratorAgent") -> Dict[str, Any]:
    c = _short_term_cfg(orch).get("compaction", {}) or {}
    return c if isinstance(c, dict) else {}


def _memory_budget_fraction(orch: "OrchestratorAgent") -> float:
    return float(_short_term_cfg(orch).get("budget_fraction", 0.70))


def _orchestrate_model(orch: "OrchestratorAgent") -> Optional[str]:
    m = _orch_config(orch).get("orchestrate_model")
    if m:
        return str(m)
    return orch._router_model()  # type: ignore[attr-defined]


def _preview(val: Any, limit: int = 600) -> str:
    return preview(val, limit=limit)


async def _do_recall_memory(
    orch: "OrchestratorAgent",
    query: str,
    user_id: Optional[str],
) -> str:
    """Execute a recall_memory tool call and return the observation string."""
    mgr = getattr(orch, "memory", None)
    if mgr is None or not getattr(mgr, "hindsight", None):
        return "(long-term memory not configured)"
    result = await mgr.recall_long_term(query, user_id=user_id)
    return result or "(no relevant memories found)"


def _format_history_text(history: List[Dict[str, Any]]) -> str:
    """Serialize a memory window into a compact text block for the LLM prompt."""
    if not history:
        return ""
    lines: List[str] = []
    for msg in history:
        role = msg.get("role", "user").capitalize()
        content = (msg.get("content") or "").strip()
        # Truncate very long individual messages so the block stays readable
        if len(content) > 800:
            content = content[:800] + "…"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def _history_and_summary_sections(
    orch: "OrchestratorAgent",
    state: "AgentState",
    model: Optional[str],
    progress_emitter: Optional["ProgressEmitter"] = None,
) -> Tuple[str, str]:
    """
    Build the (summary_section, history_section) pair for the orchestrator prompt.

    Runs a compaction preflight (folds older turns into the running summary when
    the history crosses the configured threshold), then returns:
      - summary_section:  the running summary of earlier turns (untruncated),
      - history_section:  the recent verbatim tail after the watermark.

    The current user message (last in ``state.messages``) is excluded — it appears
    explicitly as "User request" in the prompt.
    """
    memory_msgs = getattr(orch, "memory", None)
    prior_messages = list(state.messages)[:-1] if state.messages else []
    if not memory_msgs or not prior_messages:
        return "", ""

    # Preflight: compact older turns into the summary when over threshold.
    await memory_msgs.maybe_compact_conversation(
        state,
        prior_messages,
        llm_router=orch.llm_router,
        model=model,
        cfg=_compaction_cfg(orch),
        progress_emitter=progress_emitter,
    )

    watermark = int((state.metadata or {}).get(SUMMARIZED_COUNT_META_KEY, 0) or 0)
    history_window = memory_msgs.get_context_window(
        prior_messages, summarized_count=watermark, overhead_tokens=900, model=model
    )
    history_text = _format_history_text(history_window)
    history_section = f"\n\n**Conversation history:**\n{history_text}" if history_text else ""

    summary = str((state.metadata or {}).get(SUMMARY_META_KEY, "") or "").strip()
    summary_section = f"\n\n**Summary of earlier conversation:**\n{summary}" if summary else ""

    return summary_section, history_section


def _build_system_prompt(task_type: str) -> str:
    from helpers.agent_registry import LLM_EXPOSED

    bias = task_type or "auto"
    # Agent guide composed verbatim from the declarative registry — adding an
    # AgentSpec entry is all it takes to teach the LLM about a new agent.
    guide_lines = [f"  {spec.id:<15}— {spec.description}\n" for spec in LLM_EXPOSED]
    return (
        "You are the meta-orchestrator for a multi-agent data platform. "
        "Your job: decompose the user's request into subtasks, call the right specialized agents, "
        "and synthesize a clear final answer.\n\n"
        f"UI task_type hint: `{bias}` — prefer matching agents when ambiguous.\n\n"
        "Think step by step before acting. When ready, call one of the available tools.\n\n"
        "Agent guide (use with call_agent):\n"
        + "".join(guide_lines)
        + "\nSemantic layer / ontology (list_ontology / query_metric / propose_ontology):\n"
        "The session may have an ontology — the entities, relationships and metrics a human has "
        "agreed on. When a request mentions a business quantity (revenue, churn, orders, "
        "conversion, average order value, ...) or needs data from two datasets at once, call "
        "list_ontology FIRST.\n"
        "- If a metric covers it, answer with query_metric. Do NOT delegate to analysis for a "
        "number the ontology already defines — a certified definition beats a freshly written "
        "calculation, and it keeps chat, charts and exports agreeing.\n"
        "- Relationships let query_metric join datasets for you. Prefer that over asking the "
        "linking agent to join, when the relationship already exists.\n"
        "- If query_metric warns a definition is an UNCERTIFIED draft, say so plainly in your "
        "final answer and point the user at the Ontology tab. Never present a draft's number as "
        "an established figure.\n"
        "- If nothing fits, fall back to the normal agents. When the user keeps asking about a "
        "quantity that has no definition, propose_ontology drafts one for them to review — "
        "proposals are always uncertified, and only a human can certify.\n"
        f"\n{ONTOLOGY_GUIDE}\n"
        "\nRules:\n"
        "- Call agents sequentially, not in parallel.\n"
        "- When you have a complete synthesized response, simply reply in plain text without calling any tool.\n"
        "- Keep agent prompts concise and self-contained.\n"
        "- A [status: completed] observation means that subtask is finished. Continue when the "
        "user request or an activated skill still has useful unresolved objectives; synthesize "
        "and reply once the overall request is sufficiently satisfied.\n"
    )


def _activate_named_source(state: "AgentState", session_id: Optional[str], src: str) -> None:
    """Deterministically promote an orchestrator-named FILE source to the active
    dataset before dispatch, so every sub-agent (all of which read
    ``state.data_context.dataframe``) operates on it — not just via a prompt hint.

    SQL handles (``sql:`` ids) and unknown ids are left to the prompt directive.
    Best-effort: a bad id must not break the orchestration loop.
    """
    # Only real file sources can be promoted to the active dataset. SQL handles
    # and in-memory dataset ids are left to the sub-agent (linking reads
    # additional datasets directly; SQL routes via run_sql).
    if not src or src.startswith(("sql:", "dataset:")):
        return
    dc = getattr(state, "data_context", None)
    if dc is None or dc.is_sql_datasource():
        return
    try:
        from pathlib import Path  # noqa: PLC0415

        from tools.workspace_toolset import WorkspaceToolset  # noqa: PLC0415
        from workspace.manager import WorkspaceManager, dataset_stem  # noqa: PLC0415

        target_stem = dataset_stem(Path(src).name)
        current = (state.metadata or {}).get("active_dataset")
        if current and dataset_stem(current) == target_stem:
            return  # already the active dataset — nothing to do
        root = WorkspaceManager().get_or_create(
            session_id, user_id=(state.metadata or {}).get("user_id")
        )
        result = WorkspaceToolset().set_active_dataset(root, src, state=state)
        if str(result).startswith("ERROR"):
            logger.debug("[orch] set_active_dataset(%s) -> %s", src, result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[orch] activate named source failed for %s: %s", src, exc)


def _describe_workspace(state: "AgentState", session_id: Optional[str]) -> str:
    """What this session holds: sources by connector, capabilities, and files.

    Capabilities come from the connector manifests rather than being restated
    here, so this can never claim support the connectors do not have.
    """
    from datasources.contracts import (  # noqa: PLC0415
        Capability,
        FeatureAvailability,
        ProductFeature,
    )
    from datasources.feature_resolver import resolve_features  # noqa: PLC0415
    from datasources.registry import get_manifest  # noqa: PLC0415
    from datasources.source_identity import source_key  # noqa: PLC0415

    contexts = list(state.get_all_data_contexts()) if state else []
    lines: List[str] = []

    connected = [c for c in contexts if (c.metadata or {}).get("source", "upload") != "upload"]
    if connected:
        lines.append("Connected data sources (query these directly — not integrations):")
        # Same key => same engine, so a join between them can run in the source.
        groups: Dict[str, List[str]] = {}
        for ctx in connected:
            meta = ctx.metadata or {}
            connector = str(meta.get("source"))
            relation = meta.get("table") or meta.get("collection")
            label = meta.get("source_label") or relation or ctx.filename
            target = meta.get("temp_table") or relation or ctx.filename
            copies = "yes" if meta.get("temp_table") else "no"
            handle = "mongo" if connector == "mongodb" else "sql"
            lines.append(
                f"- `{handle}:{target}` — {connector}: {label} " f"(working copy created: {copies})"
            )
            key = source_key(meta)
            if key:
                groups.setdefault(key, []).append(str(label))
        for members in groups.values():
            if len(members) > 1:
                lines.append(
                    f"  · {', '.join(members)} share one connection — a join between "
                    "them runs inside the source."
                )

        seen: set[str] = set()
        for ctx in connected:
            connector = str((ctx.metadata or {}).get("source"))
            if connector in seen:
                continue
            seen.add(connector)
            try:
                decisions = resolve_features(connector)
            except KeyError:
                continue
            full, limited, missing = [], [], []
            for feature, decision in decisions.items():
                name = feature.value
                if decision.availability == FeatureAvailability.AVAILABLE:
                    full.append(name)
                elif decision.availability == FeatureAvailability.LIMITED:
                    limited.append(name)
                else:
                    missing.append(name)
            lines.append(f"{connector} supports: {', '.join(sorted(full)) or 'nothing'}")
            if limited:
                lines.append(f"{connector} limited: {', '.join(sorted(limited))}")
            if missing:
                lines.append(f"{connector} unavailable: {', '.join(sorted(missing))}")
            join = decisions.get(ProductFeature.SAME_SOURCE_JOIN)
            if join is not None and join.user_message:
                lines.append(f"{connector} joins: {join.user_message}")
            try:
                matrix = get_manifest(connector).capabilities[Capability.SAME_SOURCE_JOIN].join
            except KeyError:
                matrix = None
            if matrix is not None:
                native = [
                    item.strategy for item in matrix.strategies if item.executor.value == "source"
                ]
                streamed = [item.strategy for item in matrix.strategies if item.egress == "full"]
                lines.append(
                    f"{connector} join strategies: native in-source={native or 'none'}; "
                    f"full-input stream={streamed or 'none'}; "
                    f"join types={list(matrix.join_types)}."
                )

    files = [c for c in contexts if (c.metadata or {}).get("source", "upload") == "upload"]
    if files:
        lines.append("Uploaded datasets: " + ", ".join(str(c.filename) for c in files))

    try:
        from workspace.manager import WorkspaceManager  # noqa: PLC0415
        from workspace.sources import list_sources, render_sources_block  # noqa: PLC0415

        if state and state.session_id:
            root = WorkspaceManager().get_or_create(
                state.session_id, user_id=(state.metadata or {}).get("user_id")
            )
            block = render_sources_block(list_sources(state, root))
            if block:
                lines.append(block)
    except Exception as exc:  # noqa: BLE001 — context is best-effort
        logger.info("describe_workspace could not read the catalog: %s", exc)

    return "\n".join(lines) if lines else "This session has no data sources yet."


def _env_block(state: "AgentState", task_type: str) -> str:
    n, has_sql = 0, False
    if state and state.data_context:
        try:
            n = int(state.dataset_count) if state.dataset_count else 1
        except Exception:
            n = 1
        has_sql = state.data_context.is_sql_datasource()
    lines = [
        f"- task_type (soft): `{task_type or 'auto'}`",
        f"- datasets: {n}",
        f"- SQL active: {has_sql}",
    ]
    # Unified source catalog: every workspace file (incl. agent-created outputs),
    # every in-memory dataset, and any SQL source — the single, uniform view of
    # what the orchestrator can work on. No file gets privileged "working frame"
    # treatment; the catalog marks which source is [active] and carries schemas
    # (so linking still sees additional datasets' columns).
    try:
        from workspace.manager import WorkspaceManager  # noqa: PLC0415
        from workspace.sources import list_sources, render_sources_block  # noqa: PLC0415

        if state and state.session_id:
            root = WorkspaceManager().get_or_create(
                state.session_id, user_id=(state.metadata or {}).get("user_id")
            )
            block = render_sources_block(list_sources(state, root))
            if block:
                lines.append(block)
    except Exception:  # noqa: BLE001 — the catalog is best-effort context
        pass

    # Expose integration availability so the orchestrator can route correctly.
    # enabled_integrations=None means "all connected", [] means "none selected".
    ei = (state.metadata or {}).get("enabled_integrations") if state else None
    if ei is None:
        # None means all connected — load the registry to list names
        try:
            from mcp_infra.registry import MCP_REGISTRY  # noqa: PLC0415

            if MCP_REGISTRY:
                lines.append(
                    f"- integrations available: all connected "
                    f"({', '.join(MCP_REGISTRY.keys())})"
                )
        except Exception:
            lines.append("- integrations available: all connected (use 'integration' agent)")
    elif len(ei) > 0:
        lines.append(f"- integrations available (user-selected): {', '.join(ei)}")
    else:
        lines.append("- integrations available: none (user disabled all)")
    return "Environment:\n" + "\n".join(lines)


def _orch_call_part_id(step: int, call_index: int = 0) -> str:
    """Id of an orchestrator step's part, disambiguated for multi-tool-call steps."""
    base_id = orch_part_id(step)
    return base_id if call_index == 0 else f"{base_id}-{call_index}"


async def _emit_orch(
    progress_emitter: Optional["ProgressEmitter"],
    step: int,
    phase: str,
    thought: str,
    action: str,
    input_: Any = None,
    observation: str = "",
    call_index: int = 0,
) -> None:
    if not progress_emitter:
        return
    await progress_emitter.emit_part(
        step_part(
            agent="orchestrator",
            step=step,
            phase=phase,
            thought=thought or "",
            action=action,
            tool_input=_preview(input_, 500) if input_ is not None else "",
            observation=_preview(observation, 2000) if observation else "",
            part_id=_orch_call_part_id(step, call_index),
            parent_id=None,
        )
    )


async def run_orchestrator_react(
    orch: "OrchestratorAgent",
    *,
    task_type: str,
    message: str,
    state: "AgentState",
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    user_action: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str, List[str], Dict[str, Any], Optional[Dict[str, Any]]]:
    """Entry point for orchestration; delegates to the unified-engine loop.

    Returns:
      (response, status, task_type_label, agents_used, metadata, action_prompt)
    """
    return await run_orchestrator_unified(
        orch,
        task_type=task_type,
        message=message,
        state=state,
        session_id=session_id,
        progress_emitter=progress_emitter,
        user_action=user_action,
    )


# ===========================================================================
# Unified-engine orchestrator (agents.use_unified_engine = true)
# ===========================================================================


def _orch_observation(r: Dict[str, Any]) -> str:
    """Build the call_agent observation: '[status: ...]\\n<response>' (truncated)."""
    status = r.get("status", "completed")
    resp = str(r.get("response", "")).strip()
    if len(resp) > MAX_OBS - 30:
        resp = resp[: MAX_OBS - 30] + "…"
    return f"[status: {status}]\n{resp}" if resp else f"[status: {status}]"


def _collect_viz_artifacts(r: Dict[str, Any], viz_artifacts: List[Dict[str, Any]]) -> None:
    """Record charts a sub-agent created/updated this run (graph agent metadata)."""
    meta = r.get("metadata") or {}
    for item in list(meta.get("created_viz") or []) + list(meta.get("updated_viz") or []):
        vid = item.get("viz_id")
        if vid and all(v.get("viz_id") != vid for v in viz_artifacts):
            viz_artifacts.append(item)


def _append_viz_fences(text: str, viz_artifacts: List[Dict[str, Any]]) -> str:
    """Re-attach ```viz fences for charts the synthesized answer didn't echo.

    The fences live in sub-agent observations, which the final LLM synthesis can
    drop or truncate — re-appending by viz_id makes the chat embed deterministic.
    """
    for item in viz_artifacts:
        if item.get("kind") == "ontology":
            oid, version = str(item.get("ontology_id") or ""), item.get("version")
            if oid and oid not in text:
                payload = {"ontology_id": oid, "version": version}
                text += f"\n\n```ontology\n{json.dumps(payload)}\n```"
            continue
        vid = str(item.get("viz_id") or "")
        if vid and vid not in text:
            text += f"\n\n```viz\n{json.dumps({'viz_id': vid})}\n```"
    return text


async def _build_orch_user_block(
    orch: "OrchestratorAgent",
    message: str,
    task_type: str,
    state: "AgentState",
    progress_emitter: Optional["ProgressEmitter"],
) -> str:
    """User message for the seed transcript: env + summary + conversation history + long-term memory."""
    user_block = _env_block(state, task_type)
    model = _orchestrate_model(orch)
    memory_msgs = getattr(orch, "memory", None)
    summary_section, history_section = await _history_and_summary_sections(
        orch, state, model, progress_emitter
    )

    _user_id = str((state.metadata or {}).get("user_id", "")) or None
    _memory_allowed = long_term_memory_allowed(state)
    long_term_text = ""
    if _memory_allowed and memory_msgs and getattr(memory_msgs, "hindsight", None):
        long_term_text = await memory_msgs.recall_long_term(query=message, user_id=_user_id)
    long_term_section = (
        f"\n\n**Long-term context (past sessions — NOT the current session):**\n"
        f"⚠ These are historical notes only. Any datasets or files mentioned below "
        f"are from PREVIOUS sessions and are NOT currently uploaded or available. "
        f"Do NOT claim a file or dataset is accessible unless the Environment block above "
        f"explicitly lists it under 'working frame'.\n{long_term_text}"
        if long_term_text
        else ""
    )
    if progress_emitter and _memory_allowed:
        obs = long_term_text or "(nothing found in long-term memory)"
        await _emit_orch(
            progress_emitter,
            0,
            "end",
            "Checked long-term memory before planning",
            "recall_memory",
            message[:200],
            obs,
        )
    return (
        f"{user_block}{summary_section}{history_section}{long_term_section}"
        f"\n\n**User request:** {message}"
    )


def _orch_engine_kwargs(
    orch: "OrchestratorAgent",
    *,
    message: str,
    state: "AgentState",
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    agents_used: List[str],
    sub_agent_traces: Dict[int, List[Any]],
    pause_ctx: Dict[str, Any],
    step_counter: Dict[str, int],
    viz_artifacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Engine wiring shared by a fresh orchestration run and a HITL resume."""
    model = _orchestrate_model(orch)
    memory_allowed = long_term_memory_allowed(state)
    tools = ORCHESTRATOR_TOOLS_ANON if not memory_allowed else ORCHESTRATOR_TOOLS
    ontology_registry = OntologyToolRegistry(
        sql_agent=getattr(orch, "sql_agent", None),
        progress_emitter=progress_emitter,
        author="agent",
        model=model,
    )

    async def _tool_executor(name: str, args: Any, _call_id: str) -> str:
        step_counter["n"] += 1
        step_n = step_counter["n"]
        if name in _ONTOLOGY_TOOL_NAMES:
            result = await ontology_registry.call(tool_name=name, tool_input=args, state=state)
            # Proposals ride the same artifact channel viz uses, so the response
            # assembler can append a ```ontology fence for the review card.
            for proposal in ontology_registry.proposals:
                if proposal not in viz_artifacts:
                    viz_artifacts.append({"kind": "ontology", **proposal})
            return result

        if name == "recall_memory":
            query = args.get("query") if isinstance(args, dict) else str(args)
            _uid = str((state.metadata or {}).get("user_id", "")) or None
            if state.metadata.get("is_anonymous"):
                return "(memory recall is disabled for this session)"
            if not long_term_memory_allowed(state):
                return "(long-term memory is disabled in your account settings)"
            return await _do_recall_memory(orch, str(query), _uid)

        if name == "describe_workspace":
            return _describe_workspace(state, session_id)

        if name == "call_agent":
            inp = args if isinstance(args, dict) else {}
            ag = str(inp.get("agent", "")).lower().strip()
            sub = str(inp.get("prompt", "")).strip() or message
            dom = str(inp.get("domain", "") or "")
            if not ag:
                return "ERROR: `input` must include 'agent' and 'prompt'."
            # When the orchestrator names a specific source, make it the source the
            # sub-agent works on: promote a file source to active (deterministic —
            # every sub-agent reads state.data_context.dataframe), and bake it into
            # the prompt so the intent survives resume and covers SQL handles too.
            src = str(inp.get("input", "") or "").strip()
            if src:
                _activate_named_source(state, session_id, src)
                sub = (
                    f"{sub}\n\n[The active workspace source for this task is `{src}`. "
                    f"Work on it directly and write any new outputs as separate files.]"
                )
            # Suppress sub-agent token streaming so only the orchestrator's final
            # answer streams to the UI.
            from agents.progress import _active_token_emitter as _ate

            tok = _ate.set(None)
            try:
                r = await orch._call_agent_dispatch(  # type: ignore[attr-defined]
                    ag, sub, message, state, session_id, progress_emitter, None, domain_label=dom
                )
            finally:
                _ate.reset(tok)
            for a in r.get("agents_used", []):
                if a not in agents_used:
                    agents_used.append(a)
            _collect_viz_artifacts(r, viz_artifacts)
            _sub_trace = (r.get("metadata") or {}).get("reasoning_trace") or []
            if _sub_trace:
                sub_agent_traces[step_n] = _sub_trace
            if r.get("status") == "awaiting_user_action":
                pause_ctx.update(
                    {
                        "agent": ag,
                        "subtask_prompt": sub,
                        "domain": dom,
                        "action_prompt": r.get("action_prompt"),
                        "response": r.get("response", ""),
                    }
                )
                raise PauseSignal(r.get("action_prompt") or {}, response=r.get("response", ""))
            return _orch_observation(r)

        return f"ERROR: unknown tool '{name}'."

    from agents.progress import _active_parent_part_id

    async def _emitter(rec: ReactStepRecord) -> None:
        # A `call_agent` step is the parent of the sub-agent steps it spawns. The
        # engine fires this "start" right before invoking the tool executor (which
        # dispatches the sub-agent), so setting the ContextVar here propagates the
        # correct parentId into the sub-agent's emissions; clear it once the step ends.
        if rec.action == "call_agent":
            if rec.phase == "start":
                _active_parent_part_id.set(_orch_call_part_id(rec.step, rec.call_index))
            elif rec.phase in ("end", "pause"):
                _active_parent_part_id.set(None)
        await _emit_orch(
            progress_emitter,
            rec.step,
            rec.phase,
            rec.thought,
            rec.action,
            rec.input_preview,
            rec.observation_preview,
            call_index=rec.call_index,
        )

    async def _finalizer(text: str, _transcript: List[Message]) -> Dict[str, Any]:
        return {
            "status": "completed",
            "response": _append_viz_fences(text, viz_artifacts),
            "metadata": {},
        }

    async def _emit_token(delta: str) -> None:
        if progress_emitter:
            await progress_emitter.emit_token(delta)

    async def _stream_retract() -> None:
        if progress_emitter:
            await progress_emitter.emit_stream_retract()

    toolkit = Toolkit.from_schemas(
        tools,
        lambda name, args: _tool_executor(name, args, ""),
        known_names={"call_agent", "recall_memory", "describe_workspace"} | _ONTOLOGY_TOOL_NAMES,
        hitl={"call_agent"},
    )

    return dict(
        llm_router=orch.llm_router,
        toolkit=toolkit,
        agent_name="orchestrator",
        max_steps=_max_steps(orch),
        model=model,
        temperature=0.5,
        budget_fraction=_memory_budget_fraction(orch),
        output_token_cap=output_cap_from_config(getattr(orch, "_config", None)),
        transcript_token_budget=transcript_budget_from_config(getattr(orch, "_config", None)),
        emitter=_emitter,
        finalizer=_finalizer,
        stream_final=bool(progress_emitter),
        emit_token=_emit_token,
        stream_retract=_stream_retract,
    )


def _orch_finish(
    result: Dict[str, Any],
    state: "AgentState",
    message: str,
    agents_used: List[str],
    sub_agent_traces: Dict[int, List[Any]],
    pause_ctx: Dict[str, Any],
) -> Tuple[str, str, str, List[str], Dict[str, Any], Optional[Dict[str, Any]]]:
    if result.get("status") == "awaiting_user_action":
        state.metadata[ORCH_ENGINE_PENDING_KEY] = {
            "engine_pending": {
                "transcript": result["metadata"]["transcript"],
                "pending": result["metadata"]["pending"],
            },
            "in_flight": {
                "agent": pause_ctx.get("agent"),
                "subtask_prompt": pause_ctx.get("subtask_prompt"),
                "domain": pause_ctx.get("domain"),
            },
            "message": message,
        }
        meta = {"sub_agent_traces": sub_agent_traces, "execution_mode": "orchestrate_react"}
        if agents_used:
            meta["agents_used_cumulative"] = agents_used
        return (
            result.get("response", pause_ctx.get("response", "")),
            "awaiting_user_action",
            "orchestrated",
            agents_used,
            meta,
            pause_ctx.get("action_prompt"),
        )
    status = "completed" if result.get("status") == "completed" else "partial"
    meta = {"sub_agent_traces": sub_agent_traces, "execution_mode": "orchestrate_react"}
    if agents_used:
        meta["agents_used_cumulative"] = agents_used
    if result.get("metadata", {}).get("budget_exhausted"):
        meta["orchestrate_budget_exhausted"] = True
    return (result.get("response", ""), status, "orchestrated", agents_used, meta, None)


async def run_orchestrator_unified(
    orch: "OrchestratorAgent",
    *,
    task_type: str,
    message: str,
    state: "AgentState",
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    user_action: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str, List[str], Dict[str, Any], Optional[Dict[str, Any]]]:
    """Unified-engine orchestrator: native-tool transcript with streamed final answer."""
    is_action_turn = bool(user_action) or (message or "").strip() == "__user_action__"
    agents_used: List[str] = []
    sub_agent_traces: Dict[int, List[Any]] = {}
    pause_ctx: Dict[str, Any] = {}
    step_counter = {"n": 0}
    # Charts created/updated by sub-agents this run — re-attached to the final
    # answer as ```viz fences when the synthesis drops them.
    viz_artifacts: List[Dict[str, Any]] = []

    pend = state.metadata.get(ORCH_ENGINE_PENDING_KEY)

    # ---- Resume: a sub-agent paused on HITL; re-invoke it with the answer ----
    if is_action_turn and pend and user_action:
        in_flight = pend.get("in_flight", {})
        ag = str(in_flight.get("agent", ""))
        sub = str(in_flight.get("subtask_prompt", "")) or message
        dom = str(in_flight.get("domain", "") or "")
        orig_message = str(pend.get("message", "")) or sub

        from agents.progress import _active_token_emitter as _ate

        tok = _ate.set(None)
        try:
            r = await orch._call_agent_dispatch(  # type: ignore[attr-defined]
                ag,
                sub,
                orig_message,
                state,
                session_id,
                progress_emitter,
                user_action,
                domain_label=dom,
            )
        finally:
            _ate.reset(tok)
        for a in r.get("agents_used", []):
            if a not in agents_used:
                agents_used.append(a)
        _collect_viz_artifacts(r, viz_artifacts)
        _sub_trace = (r.get("metadata") or {}).get("reasoning_trace") or []
        if _sub_trace:
            sub_agent_traces[step_counter["n"] + 1] = _sub_trace

        if r.get("status") == "awaiting_user_action":
            # Sub-agent paused again — keep the orchestrator paused on the same call.
            meta = {"sub_agent_traces": sub_agent_traces, "execution_mode": "orchestrate_react"}
            return (
                r.get("response", ""),
                "awaiting_user_action",
                "orchestrated",
                agents_used,
                meta,
                r.get("action_prompt"),
            )

        if ag == "linking":
            # Accepting a join confirmation is the terminal operation for the
            # user's join request.  Resuming the LLM after Linking has already
            # materialized the result lets it willfully dispatch Linking a
            # second time and present a duplicate confirmation.  Return the
            # Linking agent's complete result directly; other HITL agents may
            # still need the orchestrator to continue a multi-step request.
            state.metadata.pop(ORCH_ENGINE_PENDING_KEY, None)
            meta = {
                "sub_agent_traces": sub_agent_traces,
                "execution_mode": "orchestrate_react",
                **(r.get("metadata") or {}),
            }
            return (
                r.get("response", ""),
                "completed" if r.get("status") == "completed" else "partial",
                "orchestrated",
                agents_used,
                meta,
                None,
            )

        # Sub-agent finished — resume the engine with its result as the paused
        # call_agent's native tool result.
        state.metadata.pop(ORCH_ENGINE_PENDING_KEY, None)
        kwargs = _orch_engine_kwargs(
            orch,
            message=orig_message,
            state=state,
            session_id=session_id,
            progress_emitter=progress_emitter,
            agents_used=agents_used,
            sub_agent_traces=sub_agent_traces,
            pause_ctx=pause_ctx,
            step_counter=step_counter,
            viz_artifacts=viz_artifacts,
        )
        result = await run_agent_loop(
            transcript=[],
            resume=pend["engine_pending"],
            resume_observation=_orch_observation(r),
            **kwargs,
        )
        return _orch_finish(result, state, orig_message, agents_used, sub_agent_traces, pause_ctx)

    # ---- Fresh run ----
    state.metadata.pop(ORCH_ENGINE_PENDING_KEY, None)
    system = _build_system_prompt(task_type)
    user_content = await _build_orch_user_block(orch, message, task_type, state, progress_emitter)
    transcript = [
        Message(role="system", content=system),
        Message(role="user", content=user_content),
    ]

    kwargs = _orch_engine_kwargs(
        orch,
        message=message,
        state=state,
        session_id=session_id,
        progress_emitter=progress_emitter,
        agents_used=agents_used,
        sub_agent_traces=sub_agent_traces,
        pause_ctx=pause_ctx,
        step_counter=step_counter,
        viz_artifacts=viz_artifacts,
    )
    result = await run_agent_loop(transcript=transcript, **kwargs)
    return _orch_finish(result, state, message, agents_used, sub_agent_traces, pause_ctx)

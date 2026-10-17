"""
Sub-agent invocations for the orchestrator ReAct `call_agent` tool.

Kept out of `agent.py` to avoid an oversized file; each function returns
a normalized result dict for the orchestration trace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pandas as pd

from react_engine.schema import ReactStepRecord
from tracing.trace_parts import emit_react_part

from ..handoff import handoff_to_response_metadata
from ..linking.inspect import STORE_KEY, mark_committed, public_context
from ..progress import _active_parent_part_id

if TYPE_CHECKING:
    from ..progress import ProgressEmitter
    from ..state import AgentState
    from .agent import OrchestratorAgent

logger = logging.getLogger(__name__)


def _ok(
    response: str,
    *,
    status: str = "completed",
    action_prompt: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    agents_used: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "response": response,
        "action_prompt": action_prompt,
        "metadata": metadata or {},
        "agents_used": agents_used or [],
    }


async def subagent_graph(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("graph", "Building visualization…")
    logger.info("Orchestrator call_agent → graph")
    agents_used = ["graph"]
    if state.data_context is None:
        return _ok("Please upload a dataset first before requesting a chart or visualization.")
    try:
        result = await orch.graph_agent.process_query(
            state=state,
            message=subtask,
            progress_emitter=progress_emitter,
            sql_agent=orch.sql_agent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Graph subagent failed: %s", exc)
        from helpers.sse import safe_error_message  # noqa: PLC0415

        return _ok(f"Chart generation failed: {safe_error_message(exc)}", agents_used=agents_used)

    meta = dict(result.get("metadata") or {})
    body = str(result.get("response") or "").strip()
    # Attach every created/updated visualization as a live ```viz fence — the
    # frontend renders it from the persisted spec (no baked chart data).
    created = list(meta.get("created_viz") or [])
    updated = [
        u
        for u in (meta.get("updated_viz") or [])
        if u.get("viz_id") not in {c.get("viz_id") for c in created}
    ]
    for item in created + updated:
        body += f"\n\n```viz\n{json.dumps({'viz_id': item['viz_id']})}\n```"
    if not body:
        body = "I could not generate a chart for that request."
    return _ok(body, metadata=meta, agents_used=agents_used)


async def subagent_general_analysis(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("analysis", "Running general analysis…")
    agents_used = ["analysis"]
    df = state.data_context.dataframe if state.data_context else None
    if df is None:
        return _ok("Please upload a dataset first.", agents_used=agents_used)
    result = await orch.analysis_agent.analyze(df, subtask)
    response = await orch.formatter_agent.format_data(
        result,
        question=subtask or "Dataset Analysis",
        context=f"Comprehensive analysis of {state.data_context.filename if state.data_context else 'dataset'}",
    )
    return _ok(response, metadata={}, agents_used=agents_used)


async def subagent_qa(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("qa", "Answering…")
    agents_used = ["qa"]
    df = state.data_context.dataframe if state.data_context else None
    memory_mgr = getattr(orch, "memory", None)
    if memory_mgr is not None:
        conversation_history = memory_mgr.get_context_window(
            state.messages, overhead_tokens=600, model=orch._router_model()
        )
    else:
        conversation_history = [
            {"role": msg.role, "content": msg.content} for msg in state.get_recent_messages()
        ]
    response = await orch.qa_agent.answer(
        df,
        subtask,
        conversation_history=conversation_history,
    )
    return _ok(response, metadata={}, agents_used=agents_used)


async def subagent_domain_qa(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    domain_label: str = "",
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("qa", "Looking up the answer…")
    allowed = orch._routing_config().get("domain_qa_domains", [])
    if allowed and domain_label and domain_label not in allowed:
        return _ok(
            "I'm focused on data analysis topics like statistics, SQL, machine "
            "learning, data engineering, and Python/pandas. That question seems "
            "outside my area — try a general search engine.",
            agents_used=["qa"],
        )
    response = await orch.qa_agent.answer_knowledge(subtask, state=state)
    return _ok(response, metadata={"domain": domain_label}, agents_used=["qa"])


async def subagent_analysis(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    user_action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("analysis", "Starting data analysis…")
    agents_used = ["analysis"]
    df = state.data_context.dataframe if state.data_context else None
    if df is None:
        return _ok("Please upload a dataset first to analyze.", agents_used=agents_used)
    # If the active dataset came from a join, say so as plain context instead of
    # keyword-sniffing the subtask to pick a special code path — the agent can
    # use or ignore the note.
    linking_meta = state.metadata.get("last_linking_result")
    if linking_meta and not user_action:
        note = json.dumps(
            {
                "datasets_joined": linking_meta.get("datasets_count"),
                "rows": linking_meta.get("rows"),
                "steps": linking_meta.get("step_log", []),
            },
            default=str,
        )[:600]
        subtask = (
            f"{subtask}\n\n[Context: the active dataset is the result of a dataset "
            f"join executed earlier this session: {note}]"
        )
    analysis_result = await orch.analysis_agent.process_query(
        state=state,
        message=subtask,
        user_action=user_action,
        progress_emitter=progress_emitter,
    )
    st = analysis_result.get("status", "completed")
    ap = analysis_result.get("action_prompt")
    meta = dict(analysis_result.get("metadata", {}) or {})
    meta.update(handoff_to_response_metadata(state))
    if st == "awaiting_user_action":
        return _ok(
            analysis_result.get("response", "I need your confirmation to continue."),
            status=st,
            action_prompt=ap,
            metadata=meta,
            agents_used=agents_used,
        )
    response = analysis_result.get(
        "response",
        "I completed the analysis but could not format a final response.",
    )
    return _ok(response, status="completed", metadata=meta, agents_used=agents_used)


async def subagent_cleaning(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    user_action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("cleaning", "Analyzing data quality…")
    agents_used = ["cleaning"]
    df = state.data_context.dataframe if state.data_context else None
    if df is None:
        return _ok("Please upload a dataset first to clean.", agents_used=agents_used)
    result = await orch.cleaning_agent.process_query(
        state=state,
        message=subtask,
        user_action=user_action,
        progress_emitter=progress_emitter,
    )
    response_status = result.get("status", "completed")
    action_prompt_payload = result.get("action_prompt")
    response_metadata = result.get("metadata", {}) or {}
    if response_status == "awaiting_user_action":
        return _ok(
            result.get("response", "I need your input before I continue cleaning."),
            status="awaiting_user_action",
            action_prompt=action_prompt_payload,
            metadata=response_metadata,
            agents_used=agents_used,
        )
    if response_status != "completed":
        return _ok(
            result.get("response", "Cleaning failed due to an internal error."),
            status=response_status,
            action_prompt=action_prompt_payload,
            metadata=response_metadata,
            agents_used=agents_used,
        )
    # So analysis' needs_cleaning tool / consent does not re-prompt in the same session turn.
    state.metadata["user_ran_cleaning"] = True
    state.metadata["readiness_cache_invalid"] = True
    # Completed — check analysis resume (same as legacy orchestrator)
    resume_ctx = state.metadata.pop("analysis_resume_after_cleaning", None)
    if resume_ctx:
        question = resume_ctx.get("question", subtask)
        note_prefix = resume_ctx.get("note_prefix", "")
        if state.data_context and state.data_context.is_sql_datasource() and orch.sql_agent:
            # SQL analysis runs the same agentic ReAct loop as files (run_sql/get_schema).
            follow = await orch.analysis_agent._execute_analysis(
                working_df=state.data_context.working_dataframe,
                question=question,
                state=state,
                note_prefix=note_prefix,
                progress_emitter=progress_emitter,
            )
        elif orch.csv_agent:
            follow = await orch.csv_agent.run_analysis(
                state=state,
                question=question,
                note_prefix=note_prefix,
                progress_emitter=progress_emitter,
            )
        else:
            wdf = state.data_context.working_dataframe if state.data_context else None
            follow = await orch.analysis_agent._execute_analysis(
                working_df=wdf,
                question=question,
                state=state,
                note_prefix=note_prefix,
                progress_emitter=progress_emitter,
            )
        agents_used.append("analysis")
        fm = dict(response_metadata)
        fm.update(handoff_to_response_metadata(state))
        return _ok(
            follow.get("response", "") if isinstance(follow, dict) else str(follow),
            status="completed",
            action_prompt=action_prompt_payload,
            metadata={
                **fm,
                "chained": "analysis_after_cleaning",
            },
            agents_used=agents_used,
        )
    # Analysis ReAct called needs_cleaning; cleaning had HITL and just completed — resume analysis loop.
    if state.metadata.get("analysis_react_interrupted"):
        follow = await orch.analysis_agent.resume_react_after_interrupted_cleaning(
            state, progress_emitter=progress_emitter
        )
        agents_used.append("analysis")
        fm = dict(response_metadata)
        fm.update(dict(follow.get("metadata") or {}))
        fm.update(handoff_to_response_metadata(state))
        st_f = follow.get("status", "completed")
        return _ok(
            follow.get("response", ""),
            status=st_f,
            action_prompt=follow.get("action_prompt"),
            metadata={
                **fm,
                "chained": "analysis_after_interrupted_cleaning",
            },
            agents_used=agents_used,
        )
    if progress_emitter:
        await progress_emitter.emit("cleaning", "Summarising what was done…")
    execution_summary = result.get("summary")
    if execution_summary:
        response = await orch.formatter_agent.format_data(
            execution_summary,
            question="Data Cleaning Results",
            context=(
                "The data has already been cleaned. "
                "Show a concise before/after summary. "
                "Do NOT include code blocks or recommendations."
            ),
        )
    else:
        response = result.get("response", "Cleaning completed.")
    rm = dict(response_metadata)
    rm.update(handoff_to_response_metadata(state))
    return _ok(
        response,
        status="completed",
        metadata=rm,
        agents_used=agents_used,
    )


async def subagent_integration(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("integration", "Connecting to external services…")
    logger.info("Orchestrator call_agent → integration")
    app_user_id = (state.metadata or {}).get("user_id") or ""
    enabled_integrations = (state.metadata or {}).get("enabled_integrations")
    try:
        result = await orch.integration_agent.execute(
            task=subtask,
            state=state,
            session_id=session_id,
            progress_emitter=progress_emitter,
            app_user_id=app_user_id,
            enabled_integrations=enabled_integrations,
        )
    except Exception as exc:
        logger.exception("Integration subagent failed: %s", exc)
        from helpers.sse import safe_error_message  # noqa: PLC0415

        return _ok(f"Integration failed: {safe_error_message(exc)}", agents_used=["integration"])
    return _ok(
        result.get("response", ""),
        agents_used=["integration"],
        metadata=result.get("metadata", {}),
    )


LINKING_PLAN_KEY = "last_linking_plan"


class _LinkTrace:
    """Accumulates a reasoning trace for the (non-ReAct) linking procedure.

    The linking agent is a linear pipeline, not a ReAct loop, so — unlike
    analysis/cleaning/graph — it produced no ``reasoning_trace`` and collapsed
    into a single opaque orchestrator ``call_agent`` step. This records the
    meaningful stages (inspect → decide strategy → execute) both as live trace
    parts and as the ``reasoning_trace`` the orchestrator surfaces. Under the
    structured-HITL flow the propose and execute turns are separate orchestrator
    invocations, so each surfaces its own trace segment.
    """

    def __init__(self, progress_emitter: Optional["ProgressEmitter"]) -> None:
        self._pe = progress_emitter
        self.steps: List[Dict[str, Any]] = []
        self._n = 0

    async def step(
        self,
        thought: str,
        action: str,
        *,
        input_str: str = "",
        observation: str = "",
        phase: str = "end",
    ) -> None:
        self._n += 1
        rec = ReactStepRecord(
            step=self._n,
            thought=thought,
            action=action,
            input_preview=input_str[:500],
            observation_preview=observation[:1500],
            phase=phase,
        )
        self.steps.append(
            {
                "step": self._n,
                "agent": "linking",
                "phase": phase,
                "thought": thought,
                "action": action,
                "input": rec.input_preview,
                "observation": rec.observation_preview,
            }
        )
        await emit_react_part(self._pe, "linking", rec, parent_id=_active_parent_part_id.get())


async def _register_joined_dataset(
    state: "AgentState",
    session_id: Optional[str],
    joined_df: pd.DataFrame,
    join_result: Dict[str, Any],
    source_names: List[str],
    strategy_label: str,
) -> Any:
    """Promote a join result to a new, tracked, persisted dataset.

    The joined frame gets its own identity (file_id + filename) and lineage,
    becomes the session's active working dataset (sources preserved for
    re-linking), is seeded into the workspace so the schema agents read it, and
    is snapshotted immediately. The previous in-place assignment
    (``data_context.dataframe = joined_df``) never marked the session dirty, so
    the join was lost on the next session load.
    """
    from ..state import DataContext, state_manager  # noqa: PLC0415

    file_id = uuid.uuid4().hex
    filename = f"linked_dataset_{file_id[:6]}.csv"

    col_samples: Dict[str, Any] = {}
    for col in list(joined_df.columns)[:20]:
        col_samples[col] = list(joined_df[col].dropna().astype(str).unique()[:5])

    dc = DataContext(
        file_id=file_id,
        filename=filename,
        raw_dataframe=joined_df.copy(deep=True),
        working_dataframe=joined_df,
        uploaded_at=datetime.now(),
        metadata={
            "column_samples": col_samples,
            "is_linked": True,
            "lineage": {
                "sources": source_names,
                "strategy": strategy_label,
                "steps": join_result.get("step_log", []),
            },
        },
    )

    # Promote to the active working dataset, preserving the source datasets so
    # they remain available for re-linking.
    prev = state.data_context
    if prev is not None and prev.file_id not in {d.file_id for d in state.additional_data_contexts}:
        state.additional_data_contexts.insert(0, prev)
    state.set_data_context(dc)

    # Seed the workspace + active-dataset pointer so analysis/cleaning read the
    # join instead of the previous active dataset file.
    try:
        from workspace.manager import (  # noqa: PLC0415
            WorkspaceManager,
            dataset_stem,
            seed_workspace_data,
        )

        if session_id:
            seed_uid = (state.metadata or {}).get("user_id")
            wroot = WorkspaceManager().get_or_create(session_id, user_id=seed_uid)
            seed_workspace_data(wroot, joined_df, filename)
            state.metadata["active_dataset"] = dataset_stem(filename)
    except Exception as exc:  # noqa: BLE001 — seeding is best-effort
        logger.warning("[linking] workspace seed failed: %s", exc)

    state.metadata["last_linking_result"] = {
        "file_id": file_id,
        "filename": filename,
        "rows": join_result["rows"],
        "columns": join_result["columns"],
        "datasets_count": join_result["datasets_count"],
        "steps_executed": join_result["steps_executed"],
        "step_log": join_result.get("step_log", []),
    }

    # Persist immediately so the join survives a session reload rather than
    # waiting for the 5-minute dirty-snapshot timer.
    if session_id:
        state_manager.mark_dirty(session_id)
        await state_manager.snapshot_data_async(state)

    return dc


async def _route_source_side_join(
    llm_plan: List[Dict[str, Any]],
    state: "AgentState",
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    agents_used: List[str],
    trace: "_LinkTrace",
    reasoning: str,
) -> Optional[Dict[str, Any]]:
    """Execute the join inside the customer's engine when every dataset lives there.

    Returns ``None`` to let the caller run the confirmed full-materialization
    path. Attached preview frames are never used as completed join inputs.
    """
    from agents.linking import strategy_support as ss  # noqa: PLC0415
    from agents.linking.execution import execute_native_join  # noqa: PLC0415
    from datasources.join_planner import plan_join  # noqa: PLC0415

    contexts = state.get_all_data_contexts()
    plan = plan_join(contexts)
    if not plan.source_side:
        return None

    decisions = {str(step.get("strategy") or "exact"): None for step in llm_plan} or {"exact": None}
    resolved = {name: ss.resolve_strategy(name, plan) for name in decisions}
    blocked = [(name, d) for name, d in resolved.items() if not d.available]
    if blocked:
        name, decision = blocked[0]
        await trace.step(
            f"'{name}' matching is not available for connected databases.",
            "route_join",
            observation=decision.reason,
        )
        return _ok(
            f"{decision.user_message}\n\n**Proposed plan:** {reasoning}",
            metadata={"linking_unavailable": decision.reason, "reasoning_trace": trace.steps},
            agents_used=agents_used,
        )
    if not all(d.mode == ss.SOURCE_SIDE for d in resolved.values()):
        return None

    if progress_emitter:
        await progress_emitter.emit("linking", "Joining the full tables in your database…")
    try:
        result = await asyncio.to_thread(
            execute_native_join,
            plan,
            llm_plan,
            session_id=session_id or "",
            state=state,
        )
    except Exception as exc:  # noqa: BLE001
        await trace.step(
            "Native source execution was unavailable; using the confirmed full-data fallback.",
            "route_join",
            observation=str(exc),
        )
        return None

    meta = result.metadata
    meta["temp_table_generation"] = int(meta.get("temp_table_generation") or 0) + 1
    state.metadata["last_linking_result"] = {
        "table": result.table,
        "rows": result.rows,
        "columns": result.columns,
        "datasets_count": len(plan.relations),
        "steps_executed": len(result.step_log),
        "step_log": result.step_log,
        "executed_in": result.connector,
    }
    from ..state import state_manager  # noqa: PLC0415

    if session_id:
        state_manager.mark_dirty(session_id)
        await state_manager.snapshot_data_async(state)

    await trace.step(
        "Join complete — the result is a working copy in your database.",
        "execute_join",
        observation=(
            f"{result.rows} rows × {len(result.columns)} cols written to "
            f"{result.table}; neither input left your database."
        ),
    )
    return _ok(
        f"Joined **{len(plan.relations)} tables** inside your database over the full data — "
        f"{result.rows:,} rows × {len(result.columns)} columns.\n\n"
        f"The result is saved as a working copy (`{result.table}`) and is "
        "available in the Data tab for preview, download, and further joins.",
        metadata={
            "linking": state.metadata["last_linking_result"],
            "reasoning_trace": trace.steps,
        },
        agents_used=agents_used,
    )


async def _execute_join_plan(
    orch: "OrchestratorAgent",
    datasets: List[Dict[str, Any]],
    llm_plan: List[Dict[str, Any]],
    reasoning: str,
    state: "AgentState",
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    agents_used: List[str],
    trace: "_LinkTrace",
    physical_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a confirmed join plan and make the result the active dataset."""
    contexts = state.get_all_data_contexts()
    from agents.linking.execution import validate_confirmed_physical_plan  # noqa: PLC0415
    from agents.linking.plan_validation import (  # noqa: PLC0415
        AgentJoinPlanError,
        validate_agent_join_plan,
    )
    from execution.join_inputs import JoinInputError  # noqa: PLC0415

    try:
        validate_agent_join_plan(contexts, llm_plan)
        validate_confirmed_physical_plan(contexts, llm_plan, physical_plan or {})
    except (AgentJoinPlanError, JoinInputError) as exc:
        await trace.step(
            "The confirmed join plan no longer matches the attached inputs.",
            "revalidate_join_plan",
            observation=str(exc),
        )
        return _ok(
            f"I did not run the join: {exc} Please request it again to review a fresh plan.",
            metadata={"reasoning_trace": trace.steps},
            agents_used=agents_used,
        )

    has_fuzzy = any(step.get("strategy", "exact") != "exact" for step in llm_plan)

    # Relations that live in one engine are joined by that engine over the full
    # tables. Only when that is not possible do we fall back to the loaded frames.
    routed = await _route_source_side_join(
        llm_plan, state, session_id, progress_emitter, agents_used, trace, reasoning
    )
    if routed is not None:
        return routed

    has_connected = any(
        str((getattr(ctx, "metadata", None) or {}).get("source") or "upload") != "upload"
        for ctx in contexts
    )
    if has_connected:
        from datasources.contracts.joins import CROSS_SOURCE_UNAVAILABLE_MESSAGE  # noqa: PLC0415
        from datasources.join_planner import plan_join  # noqa: PLC0415

        if not plan_join(contexts).source_side:
            await trace.step(
                "Cross-source joins are not available.",
                "execute_join",
                observation=CROSS_SOURCE_UNAVAILABLE_MESSAGE,
            )
            return _ok(
                CROSS_SOURCE_UNAVAILABLE_MESSAGE,
                metadata={"reasoning_trace": trace.steps},
                agents_used=agents_used,
            )
    execution_datasets = datasets
    if has_connected:
        from agents.linking.execution import materialize_confirmed_inputs  # noqa: PLC0415

        try:
            execution_datasets = await asyncio.to_thread(
                materialize_confirmed_inputs,
                contexts,
                physical_plan or {},
            )
        except JoinInputError as exc:
            await trace.step(
                "The confirmed full-data input plan is stale or unavailable.",
                "materialize_join_inputs",
                observation=str(exc),
            )
            return _ok(
                f"I did not run the join: {exc} Please request it again to review a fresh plan.",
                metadata={"reasoning_trace": trace.steps},
                agents_used=agents_used,
            )

    await trace.step(
        "You confirmed the join plan — executing it.",
        "execute_join",
        input_str=json.dumps(llm_plan, default=str),
        observation=reasoning,
    )
    if progress_emitter:
        await progress_emitter.emit(
            "linking", f"Executing {'fuzzy' if has_fuzzy else 'exact'} join…"
        )
    if has_fuzzy or has_connected:
        join_result = await orch.linking_agent.multi_join(
            datasets=execution_datasets,
            join_plan=llm_plan,
            default_join_type=llm_plan[0].get("join_type", "LEFT") if llm_plan else "LEFT",
            match_strategy=llm_plan[0].get("strategy", "exact") if llm_plan else "exact",
            fuzzy_threshold=llm_plan[0].get("threshold", 0.8) if llm_plan else 0.8,
        )
    else:
        from execution.joins import execute_exact_snapshot_join  # noqa: PLC0415

        join_result = await asyncio.to_thread(
            execute_exact_snapshot_join,
            state,
            llm_plan,
        )
    if "error" in join_result:
        await trace.step(
            "The join could not be executed.",
            "execute_join",
            observation=f"Error: {join_result['error']}",
        )
        return _ok(
            f"Linking failed: {join_result['error']}\n\n**LLM reasoning:** {reasoning}",
            metadata={"error": join_result, "reasoning_trace": trace.steps},
            agents_used=agents_used,
        )
    joined_df = pd.DataFrame(join_result["data"])
    # Promote the join to a properly tracked, snapshotted dataset so it survives
    # a session reload (the old in-place ``data_context.dataframe = joined_df``
    # never marked the session dirty and lost the join).
    dc = await _register_joined_dataset(
        state,
        session_id,
        joined_df,
        join_result,
        source_names=[str(d.get("name", "")) for d in execution_datasets],
        strategy_label="fuzzy matching" if has_fuzzy else "exact matching",
    )
    await trace.step(
        "Join complete — result promoted to the active working dataset.",
        "execute_join",
        observation=(
            f"{join_result['rows']} rows × {len(join_result['columns'])} cols across "
            f"{join_result['datasets_count']} datasets; saved as '{dc.filename}'."
        ),
    )
    summary = {
        "status": "Datasets linked successfully",
        "datasets_joined": join_result["datasets_count"],
        "result_shape": {
            "rows": join_result["rows"],
            "columns": len(join_result["columns"]),
        },
        "strategy_used": "fuzzy matching" if has_fuzzy else "exact matching",
        "reasoning": reasoning,
        "steps": join_result.get("step_log", []),
    }
    body = await orch.formatter_agent.format_data(
        summary,
        question="Dataset Linking Results",
        context="Summarize the join: datasets matched, shape, and strategy.",
    )
    body += (
        f"\n\nThe joined dataset is now your active working dataset "
        f"(saved as **{dc.filename}**). You can run analysis on it directly; "
        "join context (match quality, etc.) is available."
    )
    inspect_store = join_result.get("inspect")
    existing = state.metadata.get(STORE_KEY)
    if inspect_store:
        state.metadata[STORE_KEY] = mark_committed(inspect_store)
    elif existing:
        state.metadata[STORE_KEY] = mark_committed(existing)

    return _ok(
        body,
        metadata={
            "last_linking_result": state.metadata["last_linking_result"],
            "reasoning_trace": trace.steps,
            "linking_inspect": public_context(state.metadata.get(STORE_KEY)),
        },
        agents_used=agents_used,
    )


def _format_join_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _linking_confirm_prompt(
    llm_plan: List[Dict[str, Any]], reasoning: str, physical_plan: Dict[str, Any]
) -> Dict[str, Any]:
    plan_lines = "\n".join(
        f"- `{s.get('left')}`.`{s.get('left_on') or s.get('keys')}` ↔ "
        f"`{s.get('right')}`.`{s.get('right_on') or s.get('keys')}` "
        f"({s.get('strategy', 'exact')}, {s.get('join_type', 'LEFT')} join)"
        for s in llm_plan
    )
    input_plan = physical_plan.get("input_plan") or {}
    transferred = int(input_plan.get("estimated_transfer_bytes") or 0)
    total_rows = sum(int(item.get("rows") or 0) for item in input_plan.get("inputs") or [])
    if physical_plan.get("native_primary"):
        connector = str(physical_plan.get("connector") or "source")
        execution = (
            f"Primary execution: full live data inside {connector}; the result is written only "
            "to Stych scratch and only a bounded preview leaves the source."
        )
        if input_plan:
            execution += (
                f" If native execution is unavailable, the approved fallback streams "
                f"{total_rows:,} rows (about {_format_join_bytes(transferred)}) into this "
                "session and still uses the complete inputs."
            )
        elif physical_plan.get("fallback_error"):
            execution += " No automatic transfer fallback is available for this proposal."
    elif transferred:
        execution = (
            f"Execution: stream {total_rows:,} complete input rows (about "
            f"{_format_join_bytes(transferred)}) into this session, then run the selected "
            "strategy locally. No attached preview is used as the result."
        )
    else:
        execution = "Execution: full uploaded snapshots in the session; no source transfer."
    if input_plan.get("over_budget"):
        execution += (
            " This exceeds the default transfer/row approval threshold; choosing Execute join "
            "explicitly approves this one run."
        )
    return {
        "prompt_id": str(uuid.uuid4()),
        "kind": "linking_confirm",
        "question": (
            f"I propose this join plan:\n{plan_lines}\n\n"
            f"Reasoning: {reasoning or 'column compatibility'}\n\n"
            f"{execution}\n\n"
            "Execute it? Inspect the proposed matches below, then confirm. "
            "The joined result becomes your active working dataset."
        ),
        "options": [
            {"label": "Execute join", "value": "accept"},
            {"label": "Cancel", "value": "decline"},
        ],
        "allow_open_text": False,
        "context": {"join_plan": llm_plan, "physical_plan": physical_plan},
    }


async def subagent_linking(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
    user_action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if progress_emitter:
        await progress_emitter.emit("linking", "Preparing to link datasets…")
    agents_used = ["linking"]
    trace = _LinkTrace(progress_emitter)

    def _link_ret(response: str, **meta: Any) -> Dict[str, Any]:
        """Return helper that attaches the linking reasoning_trace to every exit."""
        meta.setdefault("reasoning_trace", trace.steps)
        return _ok(response, metadata=meta, agents_used=agents_used)

    datasets = state.get_all_datasets_for_linking()
    if len(datasets) < 2:
        return _link_ret(
            "To link or join datasets, I need at least **2 uploaded files**. "
            "Please upload additional datasets, then ask me to join or link them."
        )

    # ---- Resume: the user answered the join-confirmation prompt (structured
    # HITL — no free-text confirmation sniffing). ----
    if user_action and user_action.get("kind") == "linking_confirm":
        saved = state.metadata.get(LINKING_PLAN_KEY)
        if not saved or not saved.get("join_plan"):
            return _link_ret(
                "The proposed join plan is no longer available — ask me to link "
                "the datasets again and I'll propose a fresh plan.",
            )
        expected_prompt_id = saved.get("prompt_id")
        if expected_prompt_id and user_action.get("prompt_id") != expected_prompt_id:
            return _ok(
                "Please answer the latest pending join confirmation.",
                status="awaiting_user_action",
                action_prompt=saved.get("action_prompt"),
                metadata={"reasoning_trace": trace.steps},
                agents_used=agents_used,
            )
        state.metadata.pop(LINKING_PLAN_KEY, None)
        if str(user_action.get("value", "")) != "accept":
            await trace.step(
                "You declined the proposed join plan.",
                "cancel_join",
                observation="No join executed.",
            )
            state.metadata.pop(STORE_KEY, None)
            return _link_ret("Understood — I did not execute the join.")
        return await _execute_join_plan(
            orch,
            datasets,
            saved["join_plan"],
            saved.get("reasoning", ""),
            state,
            session_id,
            progress_emitter,
            agents_used,
            trace,
            saved.get("physical_plan"),
        )

    if progress_emitter:
        await progress_emitter.emit("linking", f"Analyzing {len(datasets)} datasets for linking…")

    dataset_schemas: List[Dict[str, Any]] = []
    for ds in datasets:
        df = ds["data"] if isinstance(ds["data"], pd.DataFrame) else None
        if df is not None:
            # Clip every sample value: one wide cell (embedded JSON, long text)
            # inlined per dataset would otherwise dominate the join-plan prompt.
            # Cached samples from restored sessions may predate the upload-time cap.
            cached = (ds.get("metadata") or {}).get("column_samples")
            if cached:
                sample_vals = {
                    col: [str(v)[:120] for v in cached[col][:5]]
                    for col in df.columns[:15]
                    if col in cached
                }
            else:
                sample_vals = {}
                for col in df.columns[:15]:
                    sample_vals[col] = [v[:120] for v in df[col].dropna().astype(str).unique()[:5]]
            dataset_schemas.append(
                {
                    "name": ds.get("name", "unknown"),
                    "columns": list(df.columns),
                    "dtypes": df.dtypes.astype(str).to_dict(),
                    "rows": len(df),
                    "sample_values": sample_vals,
                }
            )
    await trace.step(
        "Inspecting the uploaded datasets to find likely join keys.",
        "inspect_datasets",
        observation="; ".join(
            f"{s['name']}: {s['rows']} rows × {len(s['columns'])} cols" for s in dataset_schemas
        ),
    )
    use_message = subtask or user_message

    join_plan_prompt = "I have the following datasets that need to be linked/joined.\n\n"
    for i, schema in enumerate(dataset_schemas):
        join_plan_prompt += (
            f"Dataset {i+1}: '{schema['name']}'\n"
            f"  Columns: {schema['columns']}\n"
            f"  Types: {schema['dtypes']}\n"
            f"  Rows: {schema['rows']}\n"
            f"  Sample values: {json.dumps(schema['sample_values'], default=str)}\n\n"
        )
    join_plan_prompt += (
        f'User request: "{use_message}"\n\n'
        "Based on the column names, data types, and sample values, "
        "determine how to join these datasets. "
        "Columns may have DIFFERENT names but contain the same data "
        "(e.g. 'title' in one dataset and 'name' in another).\n\n"
        "Respond with a JSON object:\n"
        "{\n"
        '  "join_plan": [\n'
        "    {\n"
        '      "left": "<dataset name>",\n'
        '      "right": "<dataset name>",\n'
        '      "left_on": "<column in left dataset>",\n'
        '      "right_on": "<column in right dataset>",\n'
        '      "strategy": "exact" or "levenshtein" or "jaro_winkler" or "token_sort" or '
        '"token_set" or "partial_ratio" or "cosine_tfidf" or "soundex" or '
        '"metaphone" or "semantic",\n'
        '      "join_type": "INNER" or "LEFT" or "RIGHT" or "FULL OUTER",\n'
        '      "threshold": 0.8\n'
        "    }\n"
        "  ],\n"
        '  "reasoning": "<brief explanation>"\n'
        "}\n\n"
        "Rules:\n"
        "- Use fuzzy strategies when column names differ or values may have typos\n"
        "- Use exact when columns have the same name and contain IDs or codes\n"
        "- Omit threshold entirely when strategy is exact; threshold is only valid for similarity strategies\n"
        "- If the user explicitly asks for fuzzy matching, use a fuzzy strategy\n"
    )
    if progress_emitter:
        await progress_emitter.emit("linking", "Determining best join strategy…")
    join_plan_result = await orch._llm_decide(join_plan_prompt)

    if not join_plan_result or "join_plan" not in join_plan_result:
        await trace.step(
            "Could not auto-determine join keys — generating a recommendation instead.",
            "recommend_strategy",
            observation="No confident join plan; falling back to recommend_multi_join.",
        )
        recommend_result = await orch.linking_agent.recommend_multi_join(datasets=datasets)
        if "error" in recommend_result:
            return _link_ret(f"Linking analysis failed: {recommend_result['error']}")
        body = await orch.formatter_agent.format_data(
            {
                "status": "Join recommendation",
                "recommendation": recommend_result.get("recommendation", ""),
                "pairwise_analysis": recommend_result.get("pairwise_analysis", []),
            },
            question="Dataset Linking Recommendation",
            context=("I could not auto-determine join keys. Present the recommendation."),
        )
        return _link_ret(body, linking=recommend_result)

    llm_plan = join_plan_result["join_plan"]
    reasoning = join_plan_result.get("reasoning", "")
    contexts = state.get_all_data_contexts()
    try:
        from agents.linking.plan_validation import (  # noqa: PLC0415
            AgentJoinPlanError,
            resolve_agent_join_plan,
            validate_agent_join_plan,
        )

        llm_plan, join_spec = validate_agent_join_plan(contexts, llm_plan)
        resolved_plan = resolve_agent_join_plan(contexts, join_spec)
        if not resolved_plan.available:
            from datasources.contracts.joins import (  # noqa: PLC0415
                CROSS_SOURCE_UNAVAILABLE_MESSAGE,
            )

            message = next(iter(resolved_plan.caveats), None) or CROSS_SOURCE_UNAVAILABLE_MESSAGE
            await trace.step(
                "This join is not available.",
                "validate_join_plan",
                observation=message,
            )
            return _link_ret(message)
    except AgentJoinPlanError as exc:
        await trace.step(
            "The proposed join intent failed deterministic validation.",
            "validate_join_plan",
            observation=str(exc),
        )
        return _link_ret(
            f"I couldn't safely execute the proposed join plan: {exc} "
            "Please clarify the datasets or join keys and try again."
        )

    from agents.linking.execution import plan_physical_join  # noqa: PLC0415
    from execution.join_inputs import JoinInputError  # noqa: PLC0415

    try:
        physical_plan = await asyncio.to_thread(
            plan_physical_join,
            contexts,
            llm_plan,
            resolved_plan.model_dump(mode="json"),
        )
    except JoinInputError as exc:
        return _link_ret(f"I can't plan a full-data join yet: {exc}")
    has_fuzzy = any(step.get("strategy", "exact") != "exact" for step in llm_plan)
    await trace.step(
        "Chose a join strategy from the schemas and sample values; proposing it for confirmation.",
        "propose_plan",
        input_str=json.dumps(llm_plan, default=str),
        observation=(
            f"{'fuzzy' if has_fuzzy else 'exact'} matching. {reasoning}"
            if reasoning
            else f"{'fuzzy' if has_fuzzy else 'exact'} matching."
        ),
    )
    # Propose, don't execute: the join replaces the active dataset, so it pauses
    # for structured confirmation (the same HITL machinery cleaning uses).
    action_prompt = _linking_confirm_prompt(llm_plan, reasoning, physical_plan)
    state.metadata[LINKING_PLAN_KEY] = {
        "join_plan": llm_plan,
        "reasoning": reasoning,
        "physical_plan": physical_plan,
        "prompt_id": action_prompt["prompt_id"],
        "action_prompt": action_prompt,
    }
    inspect_ctx = None
    try:
        default_strategy = llm_plan[0].get("strategy", "exact") if llm_plan else "exact"
        default_threshold = llm_plan[0].get("threshold", 0.8) if llm_plan else 0.8
        preview = orch.linking_agent.preview_inspect(
            datasets=datasets,
            join_plan=llm_plan,
            default_join_type=llm_plan[0].get("join_type", "LEFT") if llm_plan else "LEFT",
            match_strategy=default_strategy,
            fuzzy_threshold=default_threshold,
        )
        if "error" not in preview:
            store = preview.get("inspect")
            state.metadata[STORE_KEY] = store
            inspect_ctx = public_context(store)
        else:
            logger.warning("Linking inspect preview failed: %s", preview.get("error"))
    except Exception:
        logger.exception("Linking inspect preview failed")
    metadata: Dict[str, Any] = {"reasoning_trace": trace.steps}
    if inspect_ctx:
        metadata["linking_inspect"] = inspect_ctx
    return _ok(
        action_prompt["question"],
        status="awaiting_user_action",
        action_prompt=action_prompt,
        metadata=metadata,
        agents_used=agents_used,
    )


async def subagent_filter(
    orch: "OrchestratorAgent",
    subtask: str,
    state: "AgentState",
    user_message: str,
    session_id: Optional[str],
    progress_emitter: Optional["ProgressEmitter"],
) -> Dict[str, Any]:
    """Create/edit saved preview filters (highlight rules, duplicate detection)."""
    logger.info("Orchestrator call_agent → filter")
    if progress_emitter:
        await progress_emitter.emit("filter", "Working on the filter…")
    result = await orch.filter_agent.execute(
        state,
        subtask or user_message,
        session_id=session_id,
        progress_emitter=progress_emitter,
    )
    return _ok(
        result.get("response", ""),
        status=result.get("status", "completed"),
        metadata=result.get("metadata") or {},
        agents_used=["filter"],
    )

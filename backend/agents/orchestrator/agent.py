"""
Orchestrator Agent for coordinating specialized agents.
Enhanced with intelligent routing and agent chaining.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from agents.analysis.agent import DataAnalysisAgent
from agents.cleaning.agent import DataCleaningAgent
from agents.csv_agent.agent import CSVAgent
from agents.filter.agent import FilterAgent
from agents.formatter.agent import FormatterAgent
from agents.graph.agent import GraphAgent
from agents.integration.agent import IntegrationAgent
from agents.linking.agent import DataLinkingAgent
from agents.orchestrator.eda_skill import (
    EDA_TASK_TYPE,
    build_eda_skill_prompt,
    eda_target_metadata,
    resolve_target_context,
)
from agents.orchestrator.orchestrator_react import run_orchestrator_react
from agents.progress import ProgressEmitter
from agents.qa.agent import QAAgent
from agents.sql_agent.agent import SQLAgent
from agents.state import AgentState, long_term_memory_allowed
from llm.infer import LLMRouter
from llm.langfuse_client import get_langfuse_tracker
from llm.usage_limit import check_usage_limit
from memory import MemoryManager, ShortTermMemory

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """
    Meta-orchestrator: ReAct loop with `call_agent` for **task
    decomposition** (see `orchestrator_react.py`). Delegates to specialized agents via
    `_call_agent_dispatch` / `subagent_calls.py`. Sub-agents own all HITL prompts;
    the orchestrator has no `question` tool. Langfuse session tags apply to nested
    work as well.
    """

    def __init__(self, llm_router: LLMRouter, config: Optional[Dict[str, Any]] = None):
        self.llm_router = llm_router
        self._config = config or {}

        # Datasource agents: SQL (DB/temp tables) and CSV (file-based)
        self.sql_agent = SQLAgent(llm_router)
        # Pass config so the analysis agent sees agents.use_unified_engine — without
        # it the flag is invisible to this agent and it silently stays on the
        # legacy loop even when the flag is on.
        self.analysis_agent = DataAnalysisAgent(
            llm_router, sql_agent=self.sql_agent, config=self._config
        )
        self.cleaning_agent = DataCleaningAgent(
            llm_router, sql_agent=self.sql_agent, config=self._config
        )
        self.csv_agent = CSVAgent(self.cleaning_agent, self.analysis_agent)
        self.cleaning_agent.csv_agent = self.csv_agent
        self.analysis_agent.csv_agent = self.csv_agent

        self.qa_agent = QAAgent(llm_router, config=config)
        self.formatter_agent = FormatterAgent(llm_router)
        self.graph_agent = GraphAgent(llm_router, config=self._config)
        self.filter_agent = FilterAgent(llm_router, config=self._config)

        # Linking agent for joining / merging multiple datasets
        db_config = self._config.get("database", {})
        self.linking_agent = DataLinkingAgent(
            llm_router,
            db_config=db_config,
            config=config,
        )

        # Short-term memory — recent verbatim tail + compaction (reads budget_fraction from config)
        mem_cfg = self._config.get("memory", {}).get("short_term", {})

        # Long-term memory (Hindsight) — opt-in via config / HINDSIGHT_ENABLED env var
        lt_cfg = self._config.get("memory", {}).get("long_term", {})
        hindsight_mem = None
        if str(lt_cfg.get("enabled", "false")).lower() in ("true", "1", "yes"):
            from memory.hindsight import HindsightLongTermMemory

            hs = lt_cfg.get("hindsight", {})
            hindsight_mem = HindsightLongTermMemory(
                base_url=str(hs.get("base_url", "http://localhost:8888")),
                bank_id_prefix=str(hs.get("bank_id_prefix", "stych")),
                recall_budget=str(hs.get("recall_budget", "low")),
                recall_max_tokens=int(hs.get("recall_max_tokens", 1024)),
                retain_extraction_mode=str(hs.get("retain_extraction_mode", "concise")),
            )
            logger.info(
                "Orchestrator: Hindsight long-term memory enabled → %s",
                hs.get("base_url", "http://localhost:8888"),
            )

        self.memory = MemoryManager(
            short_term=ShortTermMemory(
                budget_fraction=float(mem_cfg.get("budget_fraction", 0.70)),
            ),
            hindsight=hindsight_mem,
        )

        # Integration agent — discovers and calls external MCP servers
        self.integration_agent = IntegrationAgent(llm_router, config=self._config)

        logger.info("Orchestrator initialized with all specialized agents")

    async def _llm_decide(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 500,
    ) -> Dict[str, Any]:
        """Lightweight structured LLM call for routing decisions (cheap/fast model)."""
        from llm.models import LLMRequest, Message

        messages = [
            Message(
                role="system",
                content="You are a decision assistant. Respond ONLY with valid JSON — no markdown.",
            ),
            Message(role="user", content=prompt),
        ]
        req_kwargs: Dict[str, Any] = dict(
            messages=messages,
            temperature=0.0,
            max_tokens=max_tokens,
            metadata={"agent_name": "orchestrator:decide"},
        )
        if model:
            req_kwargs["model"] = model
        request = LLMRequest(**req_kwargs)
        check_usage_limit()
        try:
            response = await self.llm_router.generate(request)
            raw = (response.content or "").strip()
            for fence in ("```json", "```"):
                if raw.startswith(fence):
                    raw = raw[len(fence) :]
            if raw.endswith("```"):
                raw = raw[:-3]
            return json.loads(raw.strip())
        except Exception as exc:
            logger.warning("[Orchestrator] _llm_decide failed: %s", exc)
            return {}

    def _routing_config(self) -> Dict[str, Any]:
        return self._config.get("routing", {})

    def _router_model(self) -> Optional[str]:
        return self._routing_config().get("router_model") or None

    def _log(
        self,
        event: str,
        session_id: Optional[str] = None,
        **fields: Any,
    ) -> None:
        """Structured orchestrator logger with session context."""
        parts = [f"[Orchestrator:{event}]"]
        if session_id:
            parts.append(f"session_id={session_id}")
        for key, value in fields.items():
            parts.append(f"{key}={value}")
        logger.info(" ".join(parts))

    async def _call_agent_dispatch(
        self,
        agent_id: str,
        subtask_prompt: str,
        user_message: str,
        state: AgentState,
        session_id: Optional[str],
        progress_emitter: Optional[ProgressEmitter],
        user_action: Optional[Dict[str, Any]],
        domain_label: str = "",
    ) -> Dict[str, Any]:
        """Route a single `call_agent` tool invocation to a specialized sub-agent."""
        from helpers.agent_registry import all_dispatchable_ids, lookup

        from . import subagent_calls as sc

        spec = lookup(agent_id)
        if spec is None:
            return {
                "status": "completed",
                "response": f"Unknown agent {agent_id!r}. Use: "
                + ", ".join(all_dispatchable_ids())
                + ".",
                "action_prompt": None,
                "metadata": {},
                "agents_used": [],
            }

        wrapper = getattr(sc, spec.wrapper)
        kwargs: Dict[str, Any] = {}
        if spec.takes_user_action:
            kwargs["user_action"] = user_action
        if spec.takes_domain:
            kwargs["domain_label"] = domain_label or ""
        return await wrapper(
            self,
            subtask_prompt,
            state,
            user_message,
            session_id,
            progress_emitter,
            **kwargs,
        )

    async def execute(
        self,
        task_type: str,
        message: str,
        state: AgentState,
        session_id: Optional[str] = None,
        progress_emitter: Optional[ProgressEmitter] = None,
        user_action: Optional[Dict[str, Any]] = None,
        enabled_integrations: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        target_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute task by routing to appropriate agent(s) with intelligent chaining.

        Args:
            task_type: Type of task (analysis, cleaning, qa, graph, linking, auto)
            message: User message
            state: Agent state with conversation and data context
            session_id: Optional session ID for Langfuse correlation
            user_action: Optional user action payload
            enabled_integrations: List of MCP server_ids to enable (None = all connected)

        Returns:
            Execution result
        """
        # Propagate session ID to Langfuse so all nested LLM calls are correlated
        effective_session_id = session_id or state.session_id
        tracker = get_langfuse_tracker()
        if tracker and effective_session_id:
            tracker.set_session_id(effective_session_id)

        # Store enabled integrations in state metadata so sub-agents can access it.
        # Always write the value (including an empty list meaning "none") so that
        # subagent_calls can distinguish "all allowed" (None) from "none allowed" ([]).
        state.metadata = state.metadata or {}
        state.metadata["enabled_integrations"] = enabled_integrations

        self._log("execute_start", session_id=effective_session_id, task_type=task_type)

        async def _emit(agent: str, msg: str) -> None:
            if progress_emitter:
                await progress_emitter.emit(agent, msg)

        # The orchestrator is a process-wide singleton shared across all
        # concurrent requests, so per-request state MUST live in locals — never
        # on `self`. Storing the swapped-out context on the instance raced
        # across users (one request's finally could restore another's context).
        original_data_context = state.data_context
        original_additional_contexts = list(state.additional_data_contexts)
        restore_eda_scope = False
        try:
            await _emit("orchestrator", "Planning and delegating…")

            if (task_type or "").lower() == "filter":
                # /filter slash command: the routing decision is already made —
                # dispatch straight to the filter agent, skipping the ReAct loop.
                fr = await self._call_agent_dispatch(
                    "filter",
                    message,
                    message,
                    state,
                    effective_session_id,
                    progress_emitter,
                    user_action,
                )
                response = fr.get("response", "")
                response_status = fr.get("status", "completed")
                task_type_label = "filter"
                agents_used = fr.get("agents_used") or ["filter"]
                response_metadata = fr.get("metadata") or {}
                action_prompt_payload = fr.get("action_prompt")
            elif (task_type or "").lower() == EDA_TASK_TYPE:
                # Skill-style initial EDA: expand the pregenerated skill into an
                # explicit step-by-step prompt and run it through the normal
                # ReAct loop (the orchestrator decomposes; charts land as live
                # viz artifacts via the graph agent).
                try:
                    target_context = resolve_target_context(state, target_file_id)
                except ValueError as exc:
                    error_message = str(exc)
                    error_metadata: Dict[str, Any] = {"status": "error"}
                    if run_id:
                        error_metadata["run_id"] = run_id
                    state.add_message("assistant", error_message, metadata=error_metadata)
                    return {"error": error_message, "success": False}

                # Dataset selection is a resource-scope invariant, not a prescribed
                # workflow. Temporarily promote the requested context so every normal
                # sub-agent sees it as active while the ReAct engine remains free to
                # choose its own investigation and delegation sequence.
                if target_context is not original_data_context:
                    restore_eda_scope = True
                    state.data_context = target_context
                    state.additional_data_contexts = [
                        original_data_context if dc is target_context else dc
                        for dc in original_additional_contexts
                        if dc is not target_context or original_data_context is not None
                    ]
                skill_message = build_eda_skill_prompt(state, target_file_id)
                (
                    response,
                    response_status,
                    task_type_label,
                    agents_used,
                    response_metadata,
                    action_prompt_payload,
                ) = await run_orchestrator_react(
                    self,
                    task_type="auto",
                    message=skill_message,
                    state=state,
                    session_id=effective_session_id,
                    progress_emitter=progress_emitter,
                    user_action=user_action,
                )
                response_metadata = {
                    **(response_metadata or {}),
                    **eda_target_metadata(state, target_file_id),
                }
                task_type_label = EDA_TASK_TYPE
            else:
                (
                    response,
                    response_status,
                    task_type_label,
                    agents_used,
                    response_metadata,
                    action_prompt_payload,
                ) = await run_orchestrator_react(
                    self,
                    task_type=task_type,
                    message=message,
                    state=state,
                    session_id=effective_session_id,
                    progress_emitter=progress_emitter,
                    user_action=user_action,
                )

            # Attach the canonical, ordered trace parts accumulated by the emitter.
            # This single list (stable ids + parentId links) is the source of truth
            # for both the persisted message and the live commit fallback on the client.
            if progress_emitter is not None and getattr(progress_emitter, "parts", None):
                response_metadata = {
                    **(response_metadata or {}),
                    "parts": list(progress_emitter.parts),
                }

            assistant_metadata: Dict[str, Any] = {
                "status": response_status,
                "action_prompt": action_prompt_payload,
            }
            if run_id:
                assistant_metadata["run_id"] = run_id
            for k, v in (response_metadata or {}).items():
                if k == "status":
                    continue
                assistant_metadata[k] = v

            _is_anon = bool((state.metadata or {}).get("is_anonymous"))
            _memory_allowed = long_term_memory_allowed(state)

            # Flag this message as queued for long-term retention so the UI can
            # show a memory indicator without waiting for the async task to finish.
            # Skipped for anonymous sessions and when the user opted out in Settings.
            if self.memory.hindsight is not None and _memory_allowed:
                assistant_metadata["memory_retained"] = True

            state.add_message("assistant", response, metadata=assistant_metadata)

            # Fire-and-forget: persist this turn to long-term memory (Hindsight).
            # asyncio.create_task ensures this never blocks the response path.
            # Skipped for anonymous sessions and when the user opted out in Settings.
            if _memory_allowed:
                _user_id = str((state.metadata or {}).get("user_id", "")) or None
                _filename = state.data_context.filename if state.data_context else None
                _turn_idx = sum(1 for m in state.messages if m.role == "user")
                asyncio.create_task(
                    self.memory.retain_turn(
                        user_message=message,
                        assistant_response=response,
                        session_id=effective_session_id,
                        user_id=_user_id,
                        dataset_filename=_filename,
                        turn_index=_turn_idx,
                    )
                )

            if state.data_context is not None:
                from agents.state import state_manager as _sm

                _sm.mark_dirty(state.session_id)

            return {
                "response": response,
                "task_type": task_type_label,
                "agents_used": agents_used,
                "status": response_status,
                "action_prompt": action_prompt_payload,
                "metadata": response_metadata,
                "success": True,
            }

        except Exception as e:
            logger.error(
                f"Error in orchestrator (session: {effective_session_id}): {e}",
                exc_info=True,
            )
            from helpers.sse import safe_error_message  # noqa: PLC0415

            error_msg = safe_error_message(e)
            error_metadata = {"session_id": effective_session_id}
            if run_id:
                error_metadata["run_id"] = run_id
            state.add_message("assistant", error_msg, metadata=error_metadata)
            return {
                "error": error_msg,
                "success": False,
            }
        finally:
            # EDA temporarily promotes a target dataset for the ReAct loop.
            # Linking promotes a new joined dataset that must survive this
            # request — restoring additional_data_contexts would undo it.
            if restore_eda_scope:
                state.data_context = original_data_context
                state.additional_data_contexts = original_additional_contexts

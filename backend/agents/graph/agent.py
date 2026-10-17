"""
Graph Agent — authors and edits persistent visualizations (VizSpecs).

Complete rewrite on the shared ReAct engine: the agent is a thin spec
author/editor over the deterministic viz subsystem (backend/viz/). It creates
and updates the SAME spec entities the Visualize-tab UI edits, so agent and
human capabilities are identical by construction. Chart data is never baked
into responses — saved visualizations re-materialize over the live dataset
(full-table SQL pushdown on Postgres sessions).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from llm.infer import LLMRouter
from llm.models import Message
from react_engine.loop import output_cap_from_config, run_agent_loop
from react_engine.schema import ReactStepRecord
from tools.viz_tools import SPEC_GUIDE, VIZ_TOOL_SCHEMAS, VizReactToolRegistry, render_catalog_text
from tracing.trace_parts import emit_react_part
from viz.catalog import get_field_catalog

from ..base.agent import BaseStychAgent
from ..progress import _active_parent_part_id

if TYPE_CHECKING:  # pragma: no cover
    from ..progress import ProgressEmitter
    from ..state import AgentState

logger = logging.getLogger(__name__)

REACT_MAX_STEPS_DEFAULT = 12

_SYSTEM_PROMPT = """You are the visualization agent for a data platform. You create and edit
persistent, live visualizations by authoring VizSpec JSON through your tools — you never draw
charts inline or paste chart data into your answers.

{spec_guide}

Workflow:
1. Check the field list below. If field names are ambiguous, call list_fields.
2. For a NEW chart: optionally preview_data to sanity-check, then create_visualization.
3. To CHANGE an existing chart ("make it monthly", "stack it", "switch to a pie"): call
   list_visualizations (or use the ids below), get_visualization to read its current spec,
   modify that spec, and call update_visualization with the FULL new spec. Do NOT create a
   duplicate chart when the user refers to an existing one.
4. If a spec is rejected, read the errors, fix the spec, and retry — do not give up on the
   first validation failure. ONE exception: a type mismatch (a numeric aggregate on a text
   column) is not fixable by respecifying, because the data is the problem, not the chart.
   Warehouse sources commonly type every column as VARCHAR, so this is common and not a
   mistake you made. Stop on the first such error and say which column is the wrong type and
   that cleaning it would convert it. Never swap in a different aggregate or chart form to
   get past a type error — answering a different question than the one asked is worse than
   reporting that you cannot answer it yet. Do not start cleaning yourself: whether to change
   their data is the user's call.
5. When done, reply in plain text: one or two sentences on what the chart shows and why you
   chose that form. The chart itself is attached automatically — do NOT describe every data
   point, and do NOT include JSON in the final answer.

Choosing chart forms: trends over time → line/area with a timeUnit; category comparison →
bar (topK for long tails); part-to-whole → pie (few categories only); correlation → scatter;
distribution of one numeric → histogram; two-dimension density → heatmap. Prefer aggregated
views over raw dumps. Avoid ID-like fields as axes. Choose accessible, consistent colors with
sufficient contrast, and use color only when it communicates meaning."""


class GraphAgent(BaseStychAgent):
    """ReAct visualization agent over the viz subsystem."""

    def __init__(self, llm_router: LLMRouter, config: Optional[Dict[str, Any]] = None):
        super().__init__(
            name="GraphAgent",
            description="Creates and edits persistent data visualizations",
            llm_router=llm_router,
        )
        self._config = config or {}

    def _react_config(self) -> Dict[str, Any]:
        cfg = self._config.get("graph_react") or {}
        if not cfg and self._config.get("routing"):
            cfg = (self._config.get("routing") or {}).get("graph_react") or {}
        return cfg if isinstance(cfg, dict) else {}

    async def process_query(
        self,
        state: "AgentState",
        message: str,
        progress_emitter: Optional["ProgressEmitter"] = None,
        sql_agent: Any = None,
    ) -> Dict[str, Any]:
        """Run the visualization ReAct loop for one user request."""
        if state.data_context is None:
            return {
                "status": "completed",
                "response": "Please upload a dataset first before requesting a chart.",
                "metadata": {},
            }

        registry = VizReactToolRegistry(
            sql_agent=sql_agent, progress_emitter=progress_emitter, author="agent"
        )
        try:
            catalog = await get_field_catalog(state, sql_agent=sql_agent)
            catalog_text = render_catalog_text(catalog)
        except Exception as exc:  # noqa: BLE001 — agent can recover via list_fields
            logger.warning("[GraphAgent] catalog build failed: %s", exc)
            catalog_text = "(field catalog unavailable — call list_fields)"

        existing = await registry.call("list_visualizations", {}, state)
        system_content = (
            _SYSTEM_PROMPT.format(spec_guide=SPEC_GUIDE)
            + f"\n\n{catalog_text}\n\nExisting visualizations in this session:\n{existing}"
        )

        async def _tool_executor(name: str, args: Any, _call_id: str) -> str:
            return await registry.call(tool_name=name, tool_input=args, state=state)

        async def _emitter(rec: ReactStepRecord) -> None:
            await emit_react_part(
                progress_emitter, "graph", rec, parent_id=_active_parent_part_id.get()
            )

        async def _finalizer(text: str, _transcript: List[Message]) -> Dict[str, Any]:
            return {"status": "completed", "response": text, "metadata": {}}

        result = await run_agent_loop(
            llm_router=self.llm_router,
            tools=VIZ_TOOL_SCHEMAS,
            transcript=[
                Message(role="system", content=system_content),
                Message(role="user", content=message),
            ],
            tool_executor=_tool_executor,
            max_steps=int(self._react_config().get("react_max_steps", REACT_MAX_STEPS_DEFAULT)),
            model=self._react_config().get("graph_model") or None,
            temperature=0.3,
            output_token_cap=output_cap_from_config(self._config),
            emitter=_emitter,
            finalizer=_finalizer,
        )

        meta = result.setdefault("metadata", {})
        meta["created_viz"] = registry.created
        meta["updated_viz"] = registry.updated
        if result.get("status") == "completed" and not (registry.created or registry.updated):
            # No chart was produced — make that explicit for the orchestrator.
            meta["no_viz_produced"] = True
        return dict(result)

"""
FilterAgent: authors and edits persistent preview-filter specs conversationally.

A small ReAct loop (shared engine, no HITL) over read-only inspection tools plus
spec persistence — modeled on the CleaningAgent's unified-engine wiring, minus
everything mutation- and pause-related. Filters never modify data; the agent's
only writes are FilterSpec JSON rows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agents.base.agent import BaseStychAgent
from agents.filter.toolset import FilterToolset
from agents.progress import _active_parent_part_id
from llm.infer import LLMRouter
from llm.models import Message
from react_engine.loop import (
    output_cap_from_config,
    run_agent_loop,
    transcript_budget_from_config,
)
from react_engine.schema import ReactStepRecord
from tracing.trace_parts import emit_react_part
from workspace.manager import WorkspaceManager
from workspace.runner import WorkspaceSandboxRunner

if TYPE_CHECKING:  # pragma: no cover
    from agents.progress import ProgressEmitter
    from agents.state import AgentState

logger = logging.getLogger(__name__)

REACT_MAX_STEPS_DEFAULT = 16

_DEFAULT_PROMPT = """You are Stych's filter agent. You create and edit SAVED FILTERS: persistent \
highlight rules over the user's dataset, rendered in the Data preview tab. Filters are VIEWS — \
they never modify data.

A filter is a JSON FilterSpec with three stages:

1. "annotate" (optional) — steps that compute derived columns, joined to rows by identity. TWO \
BUILT-IN KINDS COVER MOST DUPLICATE/GROUPING REQUESTS — ALWAYS USE THEM WHEN THEY FIT; only write \
custom "pandas" code for logic they genuinely cannot express:
   - {"kind": "duplicate_values", "id": "d", "columns": ["email"], "min_count": 2} → d_count, \
d_group. Exact-match duplicates across a column set (a shared null in a key column never groups). \
THIS is the right tool for any "highlight/find duplicate rows" request — do not write custom \
pandas for it.
   - {"kind": "fuzzy_duplicates", "id": "dup", "on": "<column>", "strategy": "token_sort", \
"threshold": 0.9} → columns dup_group, dup_score, dup_match_key. Groups near-matches (typos, \
formatting differences) AND exact repeats, transitively, over one column.
   - {"kind": "pandas", "id": "z", "code": "<python>", "outputs": [{"name": "z_score", "dtype": \
"number"}]} — ONLY for logic the two builtins above cannot express (per-group stats, parsing, \
cross-column math). The code runs sandboxed: it MUST read 'filter_input.parquet' (the dataset plus \
a __row_key__ column) and MUST write 'filter_annotations/<id>.parquet' containing __row_key__ plus \
exactly the declared outputs. No network, no database, no other files. Any "*_group" output MUST \
be null/NaN for rows that are not in a group — never a sentinel like -1 or 0. A non-null group \
value is taken to mean "this row is a real match"; a sentinel corrupts group_by banding even \
though match counts still look right.
   Step ids: short lowercase [a-z0-9_].

2. "select" (optional) — which rows match. A predicate tree over base + annotation columns:
   {"op": "eq|ne|gt|ge|lt|le", "column": c, "value": scalar or {"column": other}} · \
{"op": "between", "column": c, "low": x, "high": y} · {"op": "contains|startswith|endswith|regex", \
"column": c, "pattern": p, "case_sensitive": false} · {"op": "in|not_in", "column": c, "values": \
[...]} · {"op": "is_null|not_null", "column": c} · {"op": "and|or", "children": [...]} · \
{"op": "not", "child": ...}. No arithmetic — derive computed values via an annotate step. \
Omitting "select" means every row matches.

3. "present" — how matches render:
   {"rules": [{"id": "r1", "target": "row"|"cells", "columns": [only for cells], "predicate": \
<optional, defaults to select>, "severity": "danger|warning|accent|info|success", "label": \
"<short chip>", "tooltip": "matches row {dup_match_key} at {dup_score:.0%}"}], "group_by": \
"<a *_group annotation column — bands matching pairs adjacent>", "matches_only": false}
   Tooltips interpolate {column_name} placeholders. Use group_by whenever the user needs to see \
WHICH rows pair together (duplicates), not just which rows matched.
   A "cells" rule with MULTIPLE columns must carry an explicit predicate (the schema rejects it \
otherwise) — omitting it falls back to the whole row-level select match, which paints EVERY \
listed column even when only one of them is the actual reason. For independent per-column \
conditions (e.g. "flag whichever of these columns is missing/invalid"), author ONE rule PER \
COLUMN — each with a single-item "columns" and a predicate scoped to that column alone — never \
one multi-column rule sharing one condition across columns that don't all individually satisfy it.

Top level: {"version": 1, "name": "<short display name>", "description": "...", "annotate": [...], \
"select": ..., "present": {...}}.

Plan your own approach — each LLM step is expensive, so batch what you can. Facts that keep \
filters correct:
- If the environment block lists more than one Workspace dataset, check which one the request \
actually refers to BEFORE calling any other tool. If it names or implies a dataset that isn't \
marked [active], call select_dataset first. If it's genuinely ambiguous which dataset is meant, \
ask the user — do not guess.
- Never reference a column you have not seen (get_schema / column_values / sample_rows show real \
names and value formats; inspect_data is for questions the structured tools can't answer).
- run_analyzer shows a fuzzy step's score distribution and group count — that is how thresholds \
get tuned (a "90%" ask means threshold 0.9).
- preview_filter before saving: 0 matches or nearly-all rows matching means a wrong threshold or \
predicate; a "group_sanity_warning" means the group_by column violates the null contract above — \
fix the annotate step and re-preview before saving.
- If the user is refining an existing filter, update_filter it (list_filters shows what exists) \
rather than saving a duplicate.
- After save_filter, answer in 2-4 sentences: what the filter flags, how many rows matched, and \
that it is available in the Data tab's Filters view. Never paste the spec JSON into your answer.

If the request is impossible for this dataset (missing column, wrong dtype), say so plainly and \
suggest the closest alternative — do not save a broken filter."""


class FilterAgent(BaseStychAgent):
    """ReAct spec author over the filters subsystem."""

    def __init__(self, llm_router: LLMRouter, config: Optional[Dict[str, Any]] = None):
        super().__init__(
            name="FilterAgent",
            description="Creates and edits saved data-preview filters",
            llm_router=llm_router,
        )
        self._config = config or {}
        self.workspace_manager = WorkspaceManager()
        self.workspace_runner = WorkspaceSandboxRunner(timeout_seconds=60)

    def _filter_config(self) -> Dict[str, Any]:
        return self._config.get("filter", {})

    def _system_content(self, state: "AgentState", toolset: FilterToolset) -> str:
        cfg = self._filter_config()
        prompt = cfg.get("react_agent_prompt") or _DEFAULT_PROMPT

        ctx = state.data_context
        df = ctx.working_dataframe if ctx is not None else None
        if df is None:
            env = "Environment: NO dataset loaded — tell the user to upload or connect data."
        else:
            source_kind, _params, dataset_ref = toolset.resolve_source()
            schema_summary = "\n".join(
                f"  {col} ({dtype})" for col, dtype in df.dtypes.astype(str).items()
            )
            sql_note = (
                "\nNOTE: this is a PostgreSQL source. Filters push down to the "
                "database. 'pandas' annotate steps are NOT available here — use "
                "builtins or a 'sql' step (a SELECT over {table} returning "
                "key_column + outputs) for derived columns."
                if source_kind == "sql"
                else ""
            )
            env = (
                f"Environment:\n"
                f"- Dataset: {getattr(ctx, 'filename', 'unknown')} "
                f"({df.shape[0]} rows × {df.shape[1]} columns)\n"
                f"- Source kind: {source_kind}; dataset_ref: {dataset_ref}{sql_note}\n"
                f"Columns:\n{schema_summary}"
            )
            sources_block = toolset.sources_block()
            if sources_block:
                env = f"{env}\n\n{sources_block}"
        return f"{prompt}\n\n{env}"

    async def execute(
        self,
        state: "AgentState",
        message: str,
        session_id: Optional[str] = None,
        progress_emitter: Optional["ProgressEmitter"] = None,
    ) -> Dict[str, Any]:
        """Run one filter-authoring turn; returns the sub-agent result dict."""
        effective_session_id = session_id or state.session_id
        cfg = self._filter_config()
        max_steps = int(cfg.get("react_max_steps", REACT_MAX_STEPS_DEFAULT))

        toolset = FilterToolset(
            state,
            effective_session_id,
            workspace_manager=self.workspace_manager,
            workspace_runner=self.workspace_runner,
            progress_emitter=progress_emitter,
        )

        async def _emitter(rec: ReactStepRecord) -> None:
            await emit_react_part(
                progress_emitter, "filter", rec, parent_id=_active_parent_part_id.get()
            )

        async def _finalizer(text: str, _transcript: List[Message]) -> Dict[str, Any]:
            return {
                "status": "completed",
                "response": text,
                "metadata": {"execution_mode": "react", "filters": toolset.saved},
            }

        transcript = [
            Message(role="system", content=self._system_content(state, toolset)),
            Message(role="user", content=message),
        ]
        try:
            result = await run_agent_loop(
                llm_router=self.llm_router,
                agent_name="filter",
                toolkit=toolset.toolkit,
                transcript=transcript,
                max_steps=max_steps,
                temperature=0.3,
                output_token_cap=output_cap_from_config(self._config),
                transcript_token_budget=transcript_budget_from_config(self._config),
                emitter=_emitter,
                finalizer=_finalizer,
            )
        except Exception as exc:  # noqa: BLE001 — sub-agents report, never raise
            logger.exception("FilterAgent failed")
            return {
                "status": "error",
                "response": f"Filter agent failed: {exc}",
                "metadata": {"filters": toolset.saved},
            }
        finally:
            toolset.cleanup_scratch()
        if isinstance(result, dict):
            result.setdefault("metadata", {}).setdefault("filters", toolset.saved)
        return result

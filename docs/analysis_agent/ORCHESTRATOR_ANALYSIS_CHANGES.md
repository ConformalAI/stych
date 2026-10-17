# Orchestrator Changes for Analysis Integration

> **Historical (superseded, September 2026).** The orchestrator no longer classifies intents such as `analysis_with_data`. It runs a native tool-call ReAct loop (`backend/agents/orchestrator/orchestrator_react.py` on `backend/react_engine/`) and delegates via `call_agent`. Paused sub-agent flows resume through the engine's pending record (`orchestrator_engine_pending`) and `__user_action__` replies. See [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

This document describes the orchestrator behavior for analysis routing at the time it was written.

Primary file:

- `backend/agents/orchestrator/agent.py`

Related API entrypoint:

- `backend/api/routes.py`

## Current Scope

Changes remain intentionally minimal: orchestrator routes and relays analysis workflow state, while analysis agent owns the detailed pipeline.

## Current Behavior

1. Pending analysis short-circuit

- If `state.metadata["analysis_pending"]` exists, orchestrator forces intent to `analysis_with_data`.
- This guarantees consent replies route back to `DataAnalysisAgent.process_query(...)`.

2. Stateful analysis entrypoint

- For `analysis_with_data`, orchestrator calls:
  - `self.analysis_agent.process_query(state=state, message=message, user_action=user_action)`

3. Interactive payload passthrough

- Orchestrator forwards analysis output fields directly:
  - `status`
  - `action_prompt`
  - `metadata`

4. No second formatter pass for analysis_with_data

- For this path, orchestrator returns analysis agent response text directly.
- This avoids conflicting reinterpretation of sandbox outputs.

5. Assistant message metadata

- Orchestrator stores message metadata containing:
  - `status`
  - `action_prompt`
  - `analysis_metadata` (when provided)

## Intent Routing Notes

Current `_detect_intent(...)` keeps two analysis-related paths:

- `analysis_with_data`
  - specific data question path
  - uses `process_query(...)` (readiness + consent + sandbox)

- `general_analysis`
  - overview/statistics path
  - uses `analysis_agent.analyze(...)` then formatter

## API Integration Contract

`/api/query` passes optional structured action object to orchestrator:

- `QueryRequest.user_action` -> `orchestrator.execute(..., user_action=...)`

Orchestrator response to API includes:

- `response`
- `status`
- `action_prompt`
- `metadata`

These are mapped by:

- `backend/api/routes.py`
- `backend/api/schemas.py`

## Future Configuration Points

1. Unify all analysis through one state machine

- If desired, route `general_analysis` through `process_query(...)` too.

2. Richer intent policy

- Replace keyword routing in `_detect_intent(...)` with probabilistic/LLM intent routing while preserving pending short-circuit.

3. Formatting policy tuning

- Keep current behavior for `analysis_with_data` direct responses.
- If needed later, add optional formatter post-pass guarded by strict context preservation.

# Initial EDA Skill

Initial EDA is an objective-based skill executed by Stych's main agentic engine. It is not a dedicated pipeline, endpoint, or fixed sequence of agent calls.

## Activation

The frontend creates a normal detached query run:

```json
{
  "session_id": "...",
  "message": "Run initial EDA on sales.csv",
  "task_type": "eda",
  "target_file_id": "..."
}
```

`OrchestratorAgent.execute()` resolves and temporarily activates the selected `DataContext`, expands `agents/orchestrator/eda_skill.py`, and passes that prompt to the shared ReAct engine. The primary context is restored when the run finishes.

## Skill behavior

The skill describes useful first-look objectives rather than a prescribed workflow. Depending on the dataset, the orchestrator may investigate:

- shape, types, missingness, duplicates, and data-quality concerns;
- important numeric or categorical distributions;
- relationships, correlations, segmentation, or temporal patterns;
- persistent visualizations that materially improve understanding;
- prioritized follow-up analysis or cleanup work.

The orchestrator chooses which registered agents to call, their order, and the appropriate depth. Irrelevant objectives may be skipped, interesting signals may receive more attention, and there is no fixed chart count.

## Invariants

Flexibility does not weaken these code-enforced guarantees:

- `target_file_id` scopes every sub-agent to the requested dataset;
- an unknown or retired target terminates the run as an error;
- EDA uses the same run lifecycle, ReAct engine, SSE events, tracing, limits, and persistence as other chat work;
- visualizations are persisted as normal VizSpecs and re-materialize from current data.

Regression coverage lives in `backend/tests/test_orchestrator_eda_skill.py`.

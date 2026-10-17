# Stych Backend

FastAPI multi-agent backend for the Stych data platform. See [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for the full system design.

## Quick Start

```bash
pip install -e ".[dev]"   # or: uv sync
cp .env.example .env      # fill in API keys and SECRET_KEY (see table below)
alembic upgrade head      # run database migrations
python main.py            # or: uvicorn main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

## Database Migrations (Alembic)

```bash
alembic -c alembic.ini upgrade head
alembic -c alembic.ini revision --autogenerate -m "describe change"
```

Alembic files live in `backend/alembic/`. Use Alembic for all schema changes. `init_db()` also calls `Base.metadata.create_all`, but that doesn't record migration history. Alembic resolves the DB URL from `APP_DATABASE_URL` first.

## Key Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | App secret; also derives the Fernet key for saved credentials (rotating it invalidates them) |
| `SUPERTOKENS_CONNECTION_URI` | Yes | SuperTokens Core base URL |
| `SUPERTOKENS_API_KEY` | No | SuperTokens Core API key (if core enforces it) |
| `API_DOMAIN` / `WEBSITE_DOMAIN` | Yes | Browser-facing origins for SuperTokens |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | No | Enables Google sign-in |
| `LITELLM_BASE_URL` / `LITELLM_API_KEY` | Yes* | LiteLLM proxy (default provider) |
| `LITELLM_DEFAULT_MODEL` | No | Default model |
| `OPENROUTER_API_KEY` | Yes* | Required if `llm.provider: openrouter` |
| `GOOGLE_API_KEY` | Yes* | Required if `llm.provider: google` (no native tool calling) |
| `PRIVATE_LLM_ENABLED` / `PRIVATE_LLM_BASE_URL` / `PRIVATE_LLM_MODEL` | Yes* | Self-hosted OpenAI-compatible endpoint |
| `APP_DATABASE_URL` | Yes | App Postgres URL |
| `APP_DB_SCHEMA` | No | Postgres schema for app tables (default `public`) |
| `APP_DB_REQUIRE_POSTGRES` | No | Enforce Postgres-only app DB (default `true`) |
| `STORAGE_BACKEND` | No | `local` (default) or `s3` |
| `S3_ENDPOINT_URL` / `S3_BUCKET` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | No | S3 / MinIO (when `STORAGE_BACKEND=s3`) |
| `WORKSPACE_BASE` | No | Absolute path for per-session workspaces (default `data/workspaces` under the repo root) |
| `MAX_CONCURRENT_STREAMS_PER_USER` | No | Active agent runs per user (default 3) |
| `USAGE_LIMIT_ENABLED` / `USAGE_LIMIT_USD` / `USAGE_LIMIT_EXEMPT_DOMAIN` | No | Lifetime per-user spend cap (off by default; set `USAGE_LIMIT_ENABLED=true` to enable) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | No | Langfuse tracing |
| `HINDSIGHT_ENABLED` / `HINDSIGHT_URL` | No | Long-term memory service |
| `CORS_ORIGINS` | No | Comma-separated allowed origins |
| `DEV_MODE` | No | `true` enables `POST /api/auth/dev`; refused when `ENV=production` |
| `INVALIDATE_SESSIONS_ON_RESTART` | No | Reject sessions issued before the process started |
| `ADMIN_EMAILS` / `ADMIN_USER_IDS` | No | Admin allowlists (the only admin mechanism) |

*One LLM provider must be configured.

## Project Structure

```
backend/
├── agents/
│   ├── orchestrator/
│   │   ├── agent.py               # OrchestratorAgent.execute: filter short-circuit, EDA scoping, ReAct entry
│   │   ├── orchestrator_react.py  # orchestrator tools (call_agent, recall_memory, describe_workspace, ontology) + loop wiring
│   │   ├── subagent_calls.py      # per-agent wrappers, linking confirmation flow
│   │   └── eda_skill.py           # objective-based EDA prompt
│   ├── analysis/        # Python (sandbox) / read-only SQL / Mongo analysis
│   ├── cleaning/        # data quality + mutation of workspace files or working copies (see README.md)
│   ├── graph/           # persistent VizSpec create/update
│   ├── filter/          # saved preview filters (FilterSpec)
│   ├── linking/         # join planning, validation, exact/lexical/semantic execution, inspect/viz
│   ├── qa/              # dataset Q&A and domain Q&A
│   ├── integration/     # MCP / Sheets / OpenData
│   ├── formatter/       # deterministic markdown formatting
│   ├── csv_agent/       # thin upload helpers
│   ├── sql_agent/       # per-warehouse runners, working copies, write_guard, SQL cleaning service
│   ├── mongo_agent/     # scratch-database runner + pipeline_guard
│   ├── base/            # BaseStychAgent (Google ADK subclass, LLM + streaming helpers)
│   ├── state.py         # DataContext / AgentState / StateManager (snapshots, titles)
│   ├── progress.py      # ProgressEmitter + ContextVars for tokens and trace parts
│   ├── run_manager.py   # AgentRunRegistry, RunEventBuffer
│   ├── run_executor.py  # execute_agent_run (timeouts, cancel, terminal events)
│   └── protocols.py     # optional A2A server
├── react_engine/        # shared native tool-call ReAct loop, toolkit, compaction
├── api/                 # FastAPI routers and services (runs, upload, sessions, connections, preview, diff, export, auth)
├── datasources/         # connectors, adapters/ (TableAdapter pushdown), contracts/, registry, sql_dialect, sql_readonly
├── execution/           # DuckDB kernel manager, session data plane, joins
├── viz/                 # VizSpec, validation, expr language, SQL/pandas/Mongo compilers, executor, routes
├── filters/             # FilterSpec, predicate compilers, annotation, services, routes
├── ontology/            # semantic model spec, resolve, executor, viz bridge, routes
├── tools/               # agent toolsets: workspace, analysis, cleaning, sql_write, sql_copies, viz, ontology,
│                        # linking_fuzzy, sheets, integration/MCP (run_*.py are manual driver scripts)
├── workspace/           # WorkspaceManager, WorkspaceSandboxRunner, sources catalog
├── llm/                 # LLMRouter + providers, prompt cache, Langfuse, usage tracker/limit, title generator
├── memory/              # short-term compaction, Hindsight long-term memory
├── mcp_infra/           # MCP server registry, client manager, schema patches
├── tracing/             # trace-part helpers
├── database/            # SQLAlchemy models + stores (session, connection, viz, filter, ontology, app user)
├── auth/                # SuperTokens config, dev auth
├── helpers/             # agent_registry, sse, storage, secrets_crypto, security_config, validators, oauth_*
├── config/              # config.yaml, routing_config.yaml, agents.yaml
├── scripts/             # export_contract_schemas.py (frontend contract sync)
├── alembic/             # migrations
├── tests/               # pytest suite; tests/integration for live engines
└── main.py              # app, lifespan, middleware, router registration
```

## Agent Overview

The roster is declared once in `helpers/agent_registry.py` (`AGENTS`). It generates the `call_agent` enum, the prompt's agent guide, dispatch, and the accepted `task_type` values.

### OrchestratorAgent

Every query enters through `OrchestratorAgent.execute()`. It runs the shared ReAct loop in `react_engine/loop.py` using **native LLM tool calls**. The loop ends when the model replies without a tool call, or after `react_max_steps` steps (default 20). The old JSON / `<call_agent>` XML parsing has been removed.

Tools: `call_agent(agent, prompt, input?, domain?)`, `recall_memory` (non-anonymous sessions only), `describe_workspace`, `list_ontology`, `query_metric`, `propose_ontology`.

- `input` directs a sub-agent at a named workspace source; file sources are promoted to the active dataset first.
- Only the orchestrator's final answer streams as `token` events; sub-agent steps appear as `part.updated` trace parts.
- A sub-agent that needs user input returns `awaiting_user_action`. The loop pauses with `PauseSignal` and resumes when the user replies with a `__user_action__` message.
- `task_type="filter"` goes straight to the filter agent. `task_type="eda"` scopes the run to `target_file_id` and expands the EDA skill.

### Analysis

For file sources it writes and runs Python through the workspace sandbox (`run_script`) and DuckDB. For SQL sources it uses `run_sql` (read-only statements only), and for MongoDB `run_pipeline`. It can mutate only the active private working copy. When data quality blocks analysis, it asks the user for consent to clean.

### Graph

Creates and edits persistent `VizSpec` charts through `tools/viz_tools.py` (`list_fields`, `preview_data`, `create_visualization`, `update_visualization` with optimistic `expected_version`, `list_visualizations`, `get_visualization`). Charts are stored in `visualizations` and re-run against current data through `viz/executor.py`.

### Cleaning

See [`agents/cleaning/README.md`](agents/cleaning/README.md).

### Filter

Authors saved `FilterSpec`s for the Data tab (annotate → select → present). Filters never modify data.

### Linking

LLM-planned join → validation (`plan_validation.py`) → user confirmation → execution.

- Exact joins run in the source, or in DuckDB for files.
- Lexical strategies (levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone) run in pandas.
- Semantic matching uses pgvector.
- Join types: INNER, LEFT, RIGHT, FULL OUTER. Cross-source joins are not supported.

### SQL layer (`sql_agent/`)

Not directly dispatchable by the LLM; analysis, cleaning and graph use it.

- `protocols.py` selects a runner: Postgres, SQL Server, Snowflake, Redshift, BigQuery, Databricks.
- Working copies (`working_copies.py`) live in a Stych scratch schema (default `stych_temp`). They are created lazily; reads target the live source until a copy exists.
- `write_guard.py` requires every write target to be schema-qualified inside the scratch schema and rejects anything it can't parse (fail-closed).

### BaseStychAgent

Google ADK `Agent` subclass with LLM helpers. `generate_response()` streams tokens when the `_active_token_emitter` ContextVar is set.

## Live Data Sources

All database connectors attach **by reference** (`api/remote_source.py`). Stych probes the schema and exact row count through a `TableAdapter`, stores the encrypted connection config on the `DataContext` (`storage_mode=remote_reference`), and copies no rows. Preview, filters, diffs, exports and charts run as pushdown queries. Cleaning creates complete working copies in the scratch schema, and the source is never written. Capabilities per connector: [`../docs/connector-capabilities.md`](../docs/connector-capabilities.md) (generated from `datasources/registry.py`; regenerate with `uv run python -m datasources.capability_docs --write`).

## Agent Runs and SSE

`POST /api/query/runs` creates process-owned work. `GET /api/query/runs/{id}/stream` attaches a reconnectable SSE viewer, and `POST /api/query/runs/{id}/cancel` cancels. Each user may have `MAX_CONCURRENT_STREAMS_PER_USER` (default 3) active runs and each session one queued/running run. `AgentRunRegistry` enforces admission and idempotency atomically. Runs are held in process memory and are lost on restart.

Event types are defined in `helpers/sse.py` (`SseEventType`) and exported to the frontend:

| Event | Description |
|---|---|
| `run_snapshot` | First event on every attach: streaming text, trace parts, artifacts, terminal state |
| `thinking` | Agent status message |
| `part.updated` | Upsert of a trace part (step / tool call) |
| `token` / `thinking_token` / `token_retract` | Answer text, model reasoning, retract streaming bubble |
| `viz` / `filter` / `ontology` | Artifact created or updated |
| `compaction_start` / `compaction_end` | Context compaction |
| `result` | Final response (`response`, `status`, `action_prompt`, `metadata`) |
| `error` / `timeout` / `limit_reached` / `cancelled` | Terminal states (errors sanitized by `safe_error_message`) |
| `heartbeat` | Keep-alive |
| `reasoning` | Legacy; ignored by the UI |

`ProgressEmitter` writes into `RunEventBuffer`, a bounded replay buffer (2048 events) that also maintains the live snapshot. Disconnecting an SSE viewer does not cancel the run.

## Authentication

- **SuperTokens** session auth in header-token mode (`st-access-token` header); users sync into `app_users`.
- **Email/password** by default; **Google OAuth** when `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` are configured.
- **Dev mode**: `DEV_MODE=true` enables `POST /api/auth/dev`; refused when `ENV=production`.
- Most routes depend on `get_current_user` (SuperTokens session + app user + session policy). Some legacy `/agents/*` micro-routers use bare `verify_session()`, and their `/health` endpoints are unauthenticated.

## Security Notes

- Uploads: chunked read with early size cap; missing `filename` returns 400.
- Credentials: Fernet-encrypted at rest (`helpers/secrets_crypto.py`) for saved connections, MCP credentials and sensitive snapshot metadata.
- Source protection: source tables are never written. SQL writes pass `write_guard.py`, and Mongo pipelines pass `pipeline_guard.py` (no `$out` / `$merge` / JavaScript).
- Read-only SQL: `datasources/sql_readonly.py` gates agent reads. Preview and filter predicates use bound parameters with schema-validated identifiers.
- Sandbox (`workspace/runner.py`): AST blocklist for `os`, `sys`, `subprocess`, `socket`, `shutil`, `pathlib`, `requests`, `httpx`, `urllib`, `ctypes`, `multiprocessing` and for `eval` / `exec` / `compile` / `input`. It runs `python -I` with the network denied and `open()` confined to the workspace. Limits: 60 s wall clock, 30 s CPU, 50 MB file size, 3 GB address space.
- DuckDB kernels: extensions and external access disabled, configuration locked, workspace-only paths.
- Errors: `safe_error_message()` keeps raw exception strings out of SSE errors.

## Observability

Langfuse tracing is enabled when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set. Session and user IDs live in ContextVars, so concurrent tasks never share trace state. Every LLM call is also recorded in `llm_usage_events` / `usage_ledger` with cache-aware cost estimates.

## Tests

```bash
pytest                    # unit suite (in-memory SQLite, see tests/conftest.py)
pytest tests/integration  # live-engine tests; need connector credentials in the environment
```

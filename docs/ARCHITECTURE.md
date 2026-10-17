# Stych Architecture

## System Overview

Stych is a full-stack AI data workspace. Users upload files or attach live database / warehouse tables, then chat with an orchestrator agent that delegates to specialist agents. Agent work runs as detached, process-owned runs; the browser observes them over reconnectable SSE with token streaming, live trace parts, and artifact events (visualizations, filters, ontology proposals).

```
┌──────────────────────────────────────────────────────────────────────────┐
│  React Frontend (Vite + TypeScript + Zustand)                            │
│  AppShell ─ Agent (/chat: Chat · Data · Visualize · Ontology) ─ Connectors│
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ HTTP / SSE (st-access-token header)
┌───────────────────────────────▼──────────────────────────────────────────┐
│  FastAPI Backend                                                         │
│                                                                          │
│  API layer ──▶ AgentRunRegistry ──▶ OrchestratorAgent (native tool ReAct)│
│                (replay buffer)        └─ call_agent ─▶ analysis · cleaning│
│                                          graph · filter · linking · qa   │
│                                          domain_qa · integration         │
│                                                                          │
│  Data planes: DuckDB kernel (files) · TableAdapter pushdown (databases)  │
│               workspace sandbox (Python) · scratch working copies (SQL)  │
│  Semantic layer: viz/ · filters/ · ontology/                             │
│  LLMRouter (LiteLLM · OpenRouter · Google · private) + Langfuse + usage  │
└───────┬──────────────────────┬─────────────────────────┬─────────────────┘
        │                      │                         │
  PostgreSQL (app DB)   Local / S3 / MinIO        Customer sources
  SuperTokens Core      uploads · workspaces      Postgres · SQL Server · Mongo
                        Parquet snapshots         Snowflake · Redshift · BigQuery · Databricks
```

---

## Backend Components

### 1. Application startup (`main.py`)

- Loads `.env` before any local imports (local `.env` overrides the shell; inside Docker, compose-injected variables win).
- `load_config()` reads `config/config.yaml`, deep-merges `routing_config.yaml`, and substitutes `${VAR}` / `${VAR:default}`.
- `init_supertokens()` runs before `FastAPI()` so `/auth/*` is registered.
- **Lifespan startup:** security-config validation, `init_db()`, `init_storage()`, `LLMRouter`, Langfuse, LLM health check, `OrchestratorAgent`, optional A2A server, and a background task that snapshots dirty sessions every 300 s.
- **Lifespan shutdown:** cancels the flush task, performs a final `snapshot_all_dirty()`, closes DuckDB kernels, stops A2A.
- Middleware: SuperTokens + CORS (`CORS_ORIGINS` env or yaml).

### 2. API layer (`api/`)

| File | Purpose |
|---|---|
| `routes.py` | Upload, history, session info/delete, delete-source, archive-workspace, exports, models/settings, memory, MCP integrations + OAuth, health, admin reload |
| `agent_run_routes.py` | Create / discover / cancel runs; reconnectable SSE stream |
| `auth_routes.py` | Profile, avatar, password change, usage summaries/events/CSV, session list, dev login |
| `connection_routes.py` · `connection_dispatch.py` · `connection_attach.py` | Saved per-user connections: CRUD, test, namespaces, remote tables, pinned tables, attach to a session |
| `datasource_routes.py` · `cloud_datasource_routes.py` | Direct connector endpoints for Postgres / SQL Server / MongoDB and Snowflake / Redshift / BigQuery / Databricks |
| `remote_source.py` · `dataset_attach.py` | Metadata-only attachment of live sources (schema + exact count, no row copy) |
| `preview_routes.py` · `preview_service.py` | Data tab: source listing, paged grid |
| `diff_service.py` · `sql_diff_service.py` · `mongo_diff_*` | Keyed base-vs-working-copy diffs per engine |
| `export_routes.py` · `export_service.py` · `cleaning_export_routes.py` | Streamed source export, cleaning scripts/files |
| `session_cleanup.py` · `source_lifecycle.py` | Session purge and single-source retirement |
| `dependencies.py` | `get_current_user`, `get_current_user_record`, `get_current_admin_user` |

Mounted from other packages: `viz/routes.py` (`/api/viz`), `filters/routes.py` (`/api/filters`), `ontology/routes.py` (`/api/ontology`), `agents/linking/routes.py` (`/api/linking`), plus per-agent micro-routers under `/agents/*`.

**Key endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/upload` | Upload CSV/XLSX/JSON |
| `POST` | `/api/query/runs` | Create an idempotent, process-owned agent run |
| `GET` | `/api/query/runs/session/{session_id}` | Latest run for a session |
| `GET` | `/api/query/runs/{id}/stream` | Attach or reconnect through SSE |
| `POST` | `/api/query/runs/{id}/cancel` | Cancel a run |
| `*` | `/api/connections…` | Saved connections, pinned tables, attach |
| `GET` | `/api/preview/{sources,page,diff}/{session_id}` | Data tab |
| `GET` | `/api/session/{id}` | Session metadata (empty 200 if new) |
| `DELETE` | `/api/session/{id}` | Delete session and purge its artifacts |
| `POST` | `/api/session/{id}/delete-source` | Retire one dataset/source |
| `GET` | `/api/history/{id}` | Conversation history |
| `GET` | `/api/sessions` | Session list with titles |
| `GET` | `/api/health` | Health check |

**Run admission:** `AgentRunRegistry.admit()` enforces idempotency (`client_request_id`), one queued/running run per session, and `MAX_CONCURRENT_STREAMS_PER_USER` (default 3) active runs per user. The usage quota is checked before admission (HTTP 402 when exceeded).

---

### 3. Multi-agent system (`agents/`, `react_engine/`)

#### Agent registry (`helpers/agent_registry.py`)

The declarative `AGENTS` tuple is the single source of truth for the orchestrator roster. It drives the `call_agent` tool enum, the system-prompt agent guide, dispatch, and the accepted `task_type` values (exported to the frontend).

| Agent id | Role |
|---|---|
| `analysis` | Statistics and Python execution (files) or read-only SQL / Mongo pipelines (live sources); may mutate only the active private working copy |
| `cleaning` | Data quality, dedup, type fixing, nulls; manages named working copies; only agent that can persist an approved database copy |
| `graph` | Creates and edits persistent `VizSpec` visualizations |
| `linking` | Joins / matches multiple attached datasets; pauses for structured confirmation |
| `qa` | Factual Q&A about the current dataset |
| `domain_qa` | Domain concepts (stats / SQL / ML) without a dataset |
| `filter` | Saved preview filters (highlights, duplicates); never modifies data |
| `integration` | MCP / cloud-document / open-data services |
| `general_analysis` | Hidden from the LLM; kept for internal/legacy dispatch |

`task_type` values: `auto`, `eda`, `ontology` (orchestrator modes), `analysis` / `cleaning` / `qa` / `linking` (soft routing hints), and `filter` (short-circuits directly to the filter agent).

#### ReAct engine (`react_engine/`)

A shared loop (`loop.py`) used by the orchestrator and the tool-using specialists. It uses **native LLM tool calls**; the terminal condition is an assistant turn with no tool call. The earlier JSON / `<call_agent>` XML decision parsers were removed (`react_engine/schema.py`). `compaction.py` summarizes long transcripts and emits `compaction_start` / `compaction_end`. The loop checks the usage limit before each LLM call.

Step budgets: orchestrator 20 (`react_max_steps`), cleaning 50, filter 16, graph 12.

#### OrchestratorAgent (`agents/orchestrator/`)

`OrchestratorAgent.execute()`:

1. `task_type == "filter"` → dispatch straight to the filter agent.
2. `task_type == "eda"` → resolve and temporarily activate the target `DataContext`, expand `eda_skill.py`, run the ReAct loop, restore the original context afterwards.
3. Otherwise → `run_orchestrator_react()` in `orchestrator_react.py`.

Orchestrator tools: `call_agent(agent, prompt, input?, domain?)`, `recall_memory` (not offered to anonymous sessions), `describe_workspace`, and the ontology tools (`list_ontology`, `query_metric`, `propose_ontology`).

- `input` names a workspace source; file sources are promoted to the active dataset before dispatch (`_activate_named_source`).
- Sub-agent token streaming is suppressed during `call_agent`; only the orchestrator's final answer streams as `token` events. Sub-agent steps surface as trace parts (`part.updated`).
- Human-in-the-loop: a sub-agent returning `awaiting_user_action` raises `PauseSignal`; the pending state is stored in session metadata and resumed on the user's reply (`__user_action__`).
- `subagent_calls.py` wraps each specialist and normalizes results to `{status, response, action_prompt, metadata, agents_used}`.
- Viz / ontology fences dropped by the final synthesis are re-attached from collected artifacts.

#### Specialists

- **Analysis (`analysis/`)** — file sessions run Python via `run_script` in the workspace sandbox and DuckDB; SQL sessions use `run_sql` (read-only parser) and Mongo sessions `run_pipeline`. Offers cleaning via a consent prompt when data quality blocks analysis.
- **Cleaning (`cleaning/`)** — see [`backend/agents/cleaning/README.md`](../backend/agents/cleaning/README.md).
- **Graph (`graph/`)** — authors `VizSpec`s via `viz_tools` (`list_fields`, `preview_data`, `create_visualization`, `update_visualization`, …); charts are persisted and re-materialized from live data.
- **Filter (`filter/`)** — authors `FilterSpec`s (annotate → select → present) for the Data tab.
- **Linking (`linking/`)** — LLM-planned join → user confirmation → execution. Exact joins run in the source (or DuckDB for files); lexical strategies (levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone) run in pandas; semantic matching uses pgvector. Plans are validated by `plan_validation.py`.
- **Integration (`integration/`)** — MCP tools, Google Sheets, OpenData fetches into the workspace.
- **SQL (`sql_agent/`)** — not an LLM-dispatchable agent; the shared SQL layer used by analysis/cleaning/graph. Per-warehouse runners (Postgres, SQL Server, Snowflake, Redshift, BigQuery, Databricks), named working copies (`working_copies.py`), and `write_guard.py`, which requires every write target to be schema-qualified inside the scratch schema (fail-closed).
- **Mongo (`mongo_agent/`)** — scratch-database runner and `pipeline_guard.py` (refuses `$out`, `$merge`, server-side JavaScript).
- **Formatter / CSV** — deterministic markdown formatting; thin upload helpers.
- **BaseStychAgent (`base/`)** — Google ADK `Agent` subclass with LLM helpers; streams tokens when `_active_token_emitter` is set.

---

### 4. Run lifecycle (`agents/run_manager.py`, `agents/run_executor.py`, `agents/progress.py`)

- `AgentRunRegistry` owns runs in-process. Runs survive browser disconnects but **not** a backend restart.
- `RunEventBuffer` keeps a bounded replay deque (2048 events) plus a live snapshot (streaming text, thinking text, trace parts, artifact events, terminal result). New viewers receive `run_snapshot` and then events after its sequence number.
- `execute_agent_run()` binds ContextVars (progress emitter, model override, usage limits), runs `orchestrator.execute()` under the run timeout, and maps the outcome to `result` / `error` / `timeout` / `cancelled` / `limit_reached`.
- Disconnecting a viewer never cancels a run; `POST …/cancel` cancels the task.
- Terminal runs are retained for 1 hour.

---

### 5. Session state and persistence (`agents/state.py`)

- `DataContext` — one dataset: `raw_dataframe`, `working_dataframe`, metadata. Live database sources use `storage_mode=remote_reference` with an empty frame plus column metadata and an encoded connection config.
- `AgentState` — messages, the primary `data_context`, `additional_data_contexts`, and session metadata.
- Messages are persisted to Postgres (`session_messages`) through a change callback.
- **Snapshots:** `mark_dirty()` queues a session; the 5-minute flush (and shutdown / explicit triggers) writes Parquet per dataset plus a manifest and metadata to storage. Sensitive metadata strings are Fernet-encrypted; failed writes re-queue. Restores check the owning `user_id`.
- **Titles:** after the first assistant reply, a background task generates a short session title once.

See [`UNIFIED_SOURCE_MODEL.md`](UNIFIED_SOURCE_MODEL.md) for the file-first source catalog (`workspace/sources.py`) and the planned move from an in-memory frame to a preview cache.

---

### 6. Data sources (`datasources/`)

- **Connectors** — `postgres.py`, `sqlserver.py`, `mongodb.py`, `snowflake.py` (key-pair JWT), `redshift.py` (IAM keys), `bigquery.py` (service account; legacy OAuth read-only), `databricks.py` (PAT).
- **Registry and contracts** — `registry.py` declares an exhaustive `ConnectorManifest` per connector; `feature_resolver.py` maps primitives to product features. Generated matrix: [`connector-capabilities.md`](connector-capabilities.md). Cross-source joins are unavailable for every connector.
- **Adapters (`adapters/`)** — `TableAdapter` implements schema, exact/estimated count, paged fetch with bound filter parameters, samples, and batch streaming inside the source. `DuckDBTableAdapter` serves file snapshots.
- **SQL helpers** — `sql_dialect.py` (quoting, casts, CTAS, pagination per dialect), `sql_readonly.py` (read-only statement gate and attached-asset placeholder check), `source_identity.py` and `join_planner.py` (same-source join eligibility).

### 7. Execution (`execution/`) and workspace (`workspace/`)

- **DuckDB kernel** — one in-memory kernel per `(user_id, session_id)`; extensions and external access disabled, configuration locked, inputs restricted to workspace paths.
- **Session data plane** (`session_plane.py`) — routes each dataset to the SOURCE plane (pushdown) or the DUCKDB plane.
- **Workspace manager** — per-session directories under `data/workspaces` (or `WORKSPACE_BASE`), canonical Parquet seeding, archival to storage.
- **Sandbox (`workspace/runner.py`)** — `WorkspaceSandboxRunner` executes agent Python in a `python -I` subprocess. It blocks by AST the imports `os`, `sys`, `subprocess`, `socket`, `shutil`, `pathlib`, `requests`, `httpx`, `urllib`, `ctypes`, `multiprocessing` and the calls `eval`, `exec`, `compile`, `input`. It denies the network and confines `open()` to the workspace. Defaults: 60 s wall clock, 30 s CPU, 50 MB file size, 3 GB address space. Other installed libraries are allowed.

### 8. Visualizations, filters, ontology

- **Viz (`viz/`)** — `VizSpec` v1 (Vega-Lite-inspired: data source, transforms, mark, encodings, config). The executor expands ontology metrics, resolves calculated fields (`expr.py`), validates, and compiles to SQL (`compile_sql.py`), MongoDB pipelines (`compile_mongo.py`), or DuckDB for files. `compile_pandas.py` is the parity oracle and handles multi-entity ontology joins. Results are tidy `VizData` JSON.
- **Filters (`filters/`)** — `FilterSpec` stages: annotate (duplicate / fuzzy-duplicate / agent pandas / agent SQL), select (predicate tree), present (row/cell highlights). SQL predicates use bind parameters.
- **Ontology (`ontology/`)** — per-user semantic model: entities (mapped to datasets), relationships (with cardinality / fan-out warnings), dimensions, and metrics built on the viz expression language. Supports certification, versions, and agent proposals that the user accepts or rejects.

### 9. LLM integration (`llm/`)

| Provider | `llm.provider` | Notes |
|---|---|---|
| LiteLLM (default) | `litellm` | OpenAI-compatible gateway; tool-call delta assembly; `<think>` extraction |
| OpenRouter | `openrouter` | Tool calls + fallback models |
| Google AI | `google` | No native tool calling — not suitable for tool-using agents |
| Private | `private` | Self-hosted OpenAI-compatible endpoint |

- Per-request model override via ContextVar (session `model_id` or the user's `preferred_model`).
- `prompt_cache.py` adds Anthropic cache breakpoints for Claude models.
- `langfuse_client.py` traces every call; `usage_tracker.py` records `llm_usage_events` / `usage_ledger` with cache-aware cost.
- `usage_limit.py` enforces a lifetime USD cap (`usage_limit` in config). It is checked in the ReAct loop and agent LLM helpers; direct `LLMRouter` calls (such as title generation) are not gated.
- `title_generator.py` produces 4–8-word session titles.

### 10. Memory (`memory/`) and MCP (`mcp_infra/`)

- Short-term context window with LLM compaction; optional Hindsight long-term memory (`HINDSIGHT_ENABLED`), recalled via the orchestrator's `recall_memory` tool.
- MCP registry: Google Sheets / Drive / Docs / Slides, OpenData, Notion, Yahoo Finance. Credentials are stored per user in `user_integrations` (encrypted); OAuth state lives in `oauth_pending_states`.

### 11. Storage (`helpers/storage.py`)

`STORAGE_BACKEND=local` stores under `./data/{uploads,temp,snapshots}`; `STORAGE_BACKEND=s3` targets AWS S3 or MinIO (batch deletes in chunks of 1000). Stores uploads, workspace archives, and session snapshots.

### 12. Database (`database/`)

PostgreSQL is the only supported app database. Tests use in-memory SQLite.

| Table | Purpose |
|---|---|
| `app_users` | Application users (linked by `supertokens_user_id`), handle, preferred model, memory opt-in |
| `users` | Legacy user table (superseded by `app_users`) |
| `user_quotas` | Plan and usage limits |
| `session_histories` / `session_messages` | Chat sessions (title, status, token/credit totals, `is_anonymous`) and messages |
| `uploaded_files` | Upload metadata (storage backend/key, soft delete) |
| `llm_usage_events` / `usage_ledger` | Per-call usage and billing ledger |
| `user_integrations` / `oauth_pending_states` | MCP credentials and OAuth CSRF state |
| `user_connections` / `user_connection_tables` | Saved connections (encrypted credentials, tags) and pinned tables |
| `visualizations` / `visualization_versions` | Saved VizSpecs with version history |
| `filters` / `filter_versions` | Saved FilterSpecs with version history |
| `ontologies` / `ontology_versions` | One ontology per user, with proposals and versions |

Migrations live in `backend/alembic/versions/` (`20260331_0001` … `20260828_0018`). `init_db()` also calls `Base.metadata.create_all`; treat Alembic as the schema owner.

### 13. Authentication (`auth/`)

See [`AUTHENTICATION.md`](AUTHENTICATION.md).

- SuperTokens EmailPassword + Dashboard; Google ThirdParty when `GOOGLE_OAUTH_CLIENT_ID` / `SECRET` are set; `tokenTransferMethod: "header"` on both sides.
- Sign-in syncs users into `app_users`.
- `DEV_MODE=true` registers `POST /api/auth/dev`; refused when `ENV=production`.
- Admin access comes only from `ADMIN_USER_IDS` / `ADMIN_EMAILS`.
- Optional `INVALIDATE_SESSIONS_ON_RESTART` rejects sessions issued before the process started.

### 14. Configuration

- `config/config.yaml` — structure and defaults with `${VAR:default}` substitution.
- `config/routing_config.yaml` — routing/cleaning heuristics, deep-merged at startup.
- `config/agents.yaml` — agent configuration.
- `.env` — secrets and environment overrides.

---

## Data Flows

### File upload

```
POST /api/upload (multipart)
  → validate filename, extension, size (chunked read, upload.max_size_mb)
  → parse to DataFrame → allocate session_id if needed
  → storage.save_upload + uploaded_files row
  → DataContext (+ column samples) added to the session
  → seed workspace Parquet → mark_dirty + immediate snapshot
  → { session_id, file_id, dataset_info, summary }
```

### Attaching a live source

```
POST /api/connections            → test + normalize credentials → encrypt → user_connections
POST /api/connections/{id}/attach (or …/tables/{table_id}/attach)
  → decrypt → reject if already attached
  → TableAdapter probe: schema + exact count (no rows copied)
  → DataContext(storage_mode=remote_reference) → session + snapshot
```

### Query run

```
POST /api/query/runs
  → auth + quota → AgentRunRegistry.admit (idempotent, per-session/per-user limits)
  → persist user message (skipped for __user_action__)
  → asyncio task: execute_agent_run → OrchestratorAgent.execute
GET /api/query/runs/{id}/stream
  → run_snapshot, then live events (part.updated · token · viz · filter · ontology · …)
  → terminal: result | error | timeout | cancelled | limit_reached
```

### Initial EDA

A normal run with `task_type: "eda"` and `target_file_id`. See [`INITIAL_EDA_PIPELINE.md`](INITIAL_EDA_PIPELINE.md).

### Session resume

```
GET /api/session/{id}      → metadata (or empty 200 for a new id)
GET /api/history/{id}      → messages
GET /api/query/runs/session/{id} → re-attach if a run is still queued/running
First data access          → memory miss → restore snapshot from storage
```

---

## Frontend Architecture

### Routing (`App.tsx`)

| Path | Page |
|---|---|
| `/` | Landing (can upload and queue a first question) |
| `/login` | Email/password, Google, optional dev login |
| `/oauth/callback/google` | Google OAuth callback |
| `/chat` | Agent workspace inside `AppShell` — tabs: Chat, Data, Visualize, Ontology |
| `/connectors` | Saved connections library inside `AppShell` |
| `/settings` | Profile, usage, models, memory, integrations (outside `AppShell`) |

### State (`store/useStore.ts`, `store/useAuthStore.ts`)

| Slice | Purpose |
|---|---|
| `sessionId`, `isAnonymousSession` | Current session |
| `messages[]`, `resolvedActionPromptIds` | Conversation and answered HITL prompts |
| `uploadedFiles[]` (`usePrimaryFile`) | Datasets attached to the session |
| `thinkingAgent`, `liveTraceParts`, streaming answer / thinking text, `isCompacting` | Live run UI |
| `lastVizEvent`, `lastFilterEvent`, `lastOntologyEvent`, `open*Id`, badges, `activeView` | Cross-tab artifact signals |
| `sessionModel`, `selectedModel`, `availableModels` | Model selection |
| `enabledIntegrations`, `pendingQuery`, theme, toasts | Misc UI |

`services/storage.ts` wraps localStorage (session id, drafts, theme, rail state).

### Chat flow (`hooks/useChat.ts`, `hooks/agentRunStream.ts`)

1. Build a `QueryRequest` (message or `__user_action__`, `task_type`, model, integrations, `client_request_id`, optional `target_file_id`).
2. `POST /api/query/runs`; on 409, attach to the session's latest active run.
3. Attach SSE; up to 3 attempts with exponential backoff (1 s, 2 s).
4. On session switch or mount, `GET /api/query/runs/session/{id}` re-attaches to an active run; otherwise history is loaded.
5. Stop calls `POST …/cancel`; leaving a chat aborts only the viewer.

`applyEvent` is exhaustive over the generated `SseEventType` union, so a new backend event type fails type-checking until handled.

### Components

- **Chat** — `ChatMessages` (markdown, code-block copy, action prompts, inline `VizCard` / ontology cards, legacy `chart` / `vega` fences), `AgentTrace` (live and persisted trace parts), `ChatInput` + `SlashCommandMenu`, `Sidebar` (sessions).
- **Visualize (`components/visualize/`)** — `useVizSpec` separates draft from rendered spec; drag-and-drop encoding shelves (`@dnd-kit`), `EChartsRenderer`, calculated fields, version history, "Ask this chart".
- **Data (`components/preview/`)** — `DataPreviewView` with browse / diff / filter modes, `SourcePicker`, `SpreadsheetGrid`, downloads.
- **Ontology (`components/ontology/`)** — definition list, React Flow canvas, inspector, certification, version history, metric preview.
- **Connectors (`components/connectors/`, `WarehouseConnect*`)** — catalog, filter rail, cards, per-connector forms, detail drawer (tables, tags, namespaces), delete dialog.
- **AddDataModal** — upload or attach from saved connections.

### Generated contracts

`backend/scripts/export_contract_schemas.py` writes `frontend/src/generated/schema/*.json` (VizSpec, SpecError, FieldCatalog, viz literals, SSE event types, task types). `npm run codegen` generates TypeScript into `src/generated/contracts/`, and `npm test` runs `codegen:check` first, so any drift fails. FilterSpec and OntologySpec are not generated yet.

---

## Technology Stack

### Backend

| Layer | Technology |
|---|---|
| Framework | FastAPI + Uvicorn |
| Language | Python 3.11+ |
| Agents | Google ADK base class + in-house native tool-call ReAct engine |
| LLM | LiteLLM, OpenRouter, Google GenAI, private OpenAI-compatible |
| Observability | Langfuse |
| Auth | SuperTokens |
| Database | PostgreSQL, SQLAlchemy 2, Alembic |
| Data | pandas, NumPy, PyArrow, DuckDB |
| Connectors | psycopg2, pymssql, pymongo, snowflake-connector-python, google-cloud-bigquery, redshift-connector, databricks-sql-connector |
| Matching | rapidfuzz, jellyfish, scikit-learn; pgvector (Postgres extension) for semantic matching |
| Storage | boto3 (S3 / MinIO) |
| Integrations | MCP (`mcp`), Hindsight client, `a2a` |
| Validation / config | Pydantic v2, PyYAML, python-dotenv, cryptography (Fernet) |

### Frontend

| Layer | Technology |
|---|---|
| Framework | React 18 + Vite 6 + TypeScript 5 |
| State / data | Zustand, TanStack Query, Axios, native `fetch` streaming |
| Charts | ECharts, Recharts (legacy), Vega / Vega-Lite |
| Graphs | React Flow (`@xyflow/react`), `react-force-graph-2d` |
| Auth | supertokens-auth-react |
| Styling | Tailwind CSS 3.4 |
| Tests / quality | Vitest + jsdom, ESLint 8, Prettier |

---

## Security

| Control | Implementation |
|---|---|
| Session auth | SuperTokens header tokens (`st-access-token`); most routes use `get_current_user` |
| Admin | `get_current_admin_user`, driven only by `ADMIN_USER_IDS` / `ADMIN_EMAILS` |
| Credential storage | Fernet (key derived from `SECRET_KEY`) for saved connections, MCP credentials, and sensitive snapshot metadata |
| Weak secret guard | `helpers/security_config.py` refuses weak `SECRET_KEY` in production-like environments |
| Source protection | Source tables are never written; SQL writes pass `write_guard.py` (scratch-schema only, fail-closed); Mongo pipelines pass `pipeline_guard.py` |
| Read-only SQL | `sql_readonly.py` statement gate and attached-asset placeholder check on agent query paths |
| Preview / filter SQL | Bound parameters and schema-validated, dialect-quoted identifiers |
| Sandbox | AST import/call blocklist, `python -I`, network denied, workspace-confined `open`, rlimits |
| DuckDB | Extensions and external access disabled; workspace path policy |
| Upload validation | Filename, extension, chunked size check |
| Error exposure | `safe_error_message()` for SSE errors |
| Session isolation | Data keyed by `session_id` + `user_id`; snapshot restore checks ownership |
| Dev mode | `DEV_MODE=true` only; refused when `ENV=production` |

---

## Code Quality

### Backend

```bash
cd backend
ruff check .
black --line-length 100 .
pytest
```

### Frontend

```bash
cd frontend
npm run check   # type-check + lint + format:check + test (test runs codegen:check first)
```

---

## Agent-to-Agent (A2A) Protocol

`agents/protocols.py` wraps the orchestrator as an A2A server (default `0.0.0.0:8001`) when `a2a.enabled` is set in config. Remote agents can be registered under `a2a.remote_agents`. The server has no auth of its own, so don't expose it publicly.

---

## Roadmap

| Area | Status |
|---|---|
| File uploads (CSV / XLSX / JSON) | ✅ Done |
| PostgreSQL | ✅ Production |
| SQL Server, MongoDB, Snowflake, Redshift, BigQuery, Databricks | ✅ Beta |
| Saved connections library | ✅ Done |
| Pushdown preview / filter / diff / export | ✅ Done |
| Persistent visualizations (VizSpec + ECharts) | ✅ Done |
| Saved filters | ✅ Done |
| Ontology / semantic layer | ✅ Done |
| Record linkage (exact + lexical + semantic) | ✅ Done |
| Detached, reconnectable agent runs | ✅ Done |
| MCP integrations + long-term memory | ✅ Done |
| Cross-source joins | 🔄 Planned |
| DuckDB as the universal engine (see `UNIFIED_SOURCE_MODEL.md`) | 🔄 Planned |
| Durable runs across backend restarts | 🔄 Planned |
| Filter / ontology contract codegen | 🔄 Planned |
| Kubernetes deployment | 🔄 Planned |
| Predictive modeling agent | 🔄 Planned |

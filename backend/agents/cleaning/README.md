# Cleaning Agent

`DataCleaningAgent` (`agent.py`) finds and fixes data-quality problems. It runs on the shared native tool-call ReAct engine (`backend/react_engine/loop.py`, `run_agent_loop`), so the LLM chooses tools through structured tool calls. The loop ends when the model replies without a tool call.

What it can change depends on the source:

- **File sources** — files in the session workspace (the active dataset is a Parquet file), edited by sandboxed Python.
- **SQL sources** (Postgres, SQL Server, Snowflake, Redshift, BigQuery, Databricks) — Stych-owned **working copies** in a scratch schema (default `stych_temp`).
- **MongoDB** — working-copy collections in a Stych scratch database.

The source table or collection is never modified. Writing a persistent database object outside the scratch area requires explicit, per-operation user approval.

---

## Architecture Overview

```
Orchestrator call_agent("cleaning", …)
    │
    ▼
DataCleaningAgent._execute_react()
    │
    ├─ pending HITL record? ──▶ _resume_engine()   (question answer / permission decision)
    │
    ▼
_unified_setup() → Toolkit (tools gated by source) → run_agent_loop()   ← up to react_max_steps (50)
    │
    ├─ tool "question"                      → PauseSignal → awaiting_user_action
    ├─ tool in _PERSISTENT_PERMISSION_TOOLS → PauseSignal → yes/no approval prompt
    └─ any other tool                       → CleaningReactToolRegistry.call()
```

---

## Files

| File | Responsibility |
|---|---|
| `agent.py` | Prompt construction, tool gating, unified-engine setup, HITL pause/resume, write-back enforcement |
| `../../tools/cleaning_tools.py` | `CleaningReactToolRegistry`, SQL and Mongo tool schemas + handlers (kept together so parameter names can't drift) |
| `../../tools/workspace_toolset.py` | Workspace file tools shared by all sub-agents |
| `../../tools/sql_copies.py` | Working-copy lifecycle tools |
| `../../tools/sql_write.py` | `sql_write_temp` (DML/DDL on the scratch working copy only) |
| `../sql_agent/` | Per-warehouse runners, `working_copies.py`, `write_guard.py`, `cleaning.py` (`SQLCleaningService`) |
| `../mongo_agent/` | Mongo scratch runner and `pipeline_guard.py` |
| `../../workspace/runner.py` | `WorkspaceSandboxRunner` (sandboxed subprocess) |
| `../../workspace/manager.py` | `WorkspaceManager` (per-session workspace, canonical Parquet seeding, archival) |
| `exporter.py` | README generation for exported cleaning scripts |
| `routes.py` | `POST /agents/cleaning/quality`, `GET /agents/cleaning/health` |

---

## Available Tools

### Workspace tools (file and SQL sessions)

| Tool | What it does |
|---|---|
| `list_files` | List workspace files |
| `read_file` | Read a workspace file (preview) |
| `write_file` / `edit_file` / `delete_file` | Create, patch, or remove workspace files |
| `run_script` | Run Python in the sandbox; reloads the active dataset afterwards |
| `set_active_dataset` | Make a workspace file the active dataset |

### SQL tools (SQL sessions only)

| Tool | Requires approval | What it does |
|---|---|---|
| `sql_read` | No | Read-only SELECT / WITH against the active target |
| `sql_preview` | No | Preview rows/columns of the active target |
| `sql_write_temp` | No | DML/DDL on the active scratch working copy |
| `sql_materialize_copy` | **Yes** | Persist the working copy as a database table |
| `sql_write_copy` | **Yes** | Write changes to the persistent copy table |
| `create_copy_table` | **Yes** | Create a new copy of the source table |
| `create_view` | **Yes** | Create or replace a SQL view |

### MongoDB tools (Mongo sessions only)

`mongo_read` (read-only aggregation; `$lookup` / `$unionWith` / server-side JavaScript refused), `mongo_update`, `mongo_delete`, `mongo_deduplicate`.

### Working-copy tools (SQL and Mongo sessions)

`create_working_copy`, `switch_working_copy`, `drop_working_copy`, `list_working_copies`, `diff_working_copies`.

### Control tool

| Tool | What it does |
|---|---|
| `question` | Pause and ask the user a multiple-choice (+ free text) question |

The registry refuses SQL or Mongo tools when the session has no matching connection, even if the model calls them.

---

## Human-in-the-Loop (HITL)

Both pauses raise `PauseSignal`. The engine record is stored under `state.metadata["cleaning_engine_pending"]`, and the agent returns `status: "awaiting_user_action"` with an `action_prompt`. The orchestrator relays the prompt and routes the user's `__user_action__` reply back.

### Question (`kind: cleaning_react_question`)

The agent needs clarification. `_resume_engine()` checks that the reply matches the pending `prompt_id`, then feeds the answer back as the tool result and continues the loop.

### Permission (`kind: cleaning_react_permission`)

`sql_materialize_copy`, `sql_write_copy`, `create_copy_table` and `create_view` are intercepted **before** execution, whatever arguments the model sends.

- **Accept:** `_resume_engine()` adds `approved: true` and calls the tool directly. The model never sees an approved payload, so it can't approve itself.
- **Deny:** the result is recorded as `User denied permission.`

Each decision is stored as a receipt keyed by operation, so the same request is not asked twice in one run.

### Write-back enforcement

If the user asked to persist results (for example "write it back to the database") and the model finishes without materializing, `_enforce_writeback` nudges it once to call `sql_materialize_copy`. That call then goes through the permission gate.

---

## Sandbox (`WorkspaceSandboxRunner`)

`run_script` executes agent-written Python in a subprocess started with `python -I`.

- **Blocked imports (AST check):** `os`, `sys`, `subprocess`, `socket`, `shutil`, `pathlib`, `requests`, `httpx`, `urllib`, `ctypes`, `multiprocessing`.
- **Blocked calls (AST check):** `eval`, `exec`, `compile`, `input`.
- **Runtime:** network denied (socket stubs), `open()` confined to the session workspace, builtins minus `eval` / `exec` / `compile` / `input` / `breakpoint`.
- **Limits:** 60 s wall clock (hardcoded by the cleaning agent), 30 s CPU (`RLIMIT_CPU`), 50 MB per file (`RLIMIT_FSIZE`), 3 GB address space (`RLIMIT_AS`).
- **Packages:** any installed library that isn't blocked (pandas, numpy, scipy, scikit-learn, …).
- **Errors:** long output keeps both head and tail (`head_tail_preview`), so the final exception line is always visible.

---

## Session Workspace

Each session gets its own directory. Base path precedence:

1. Explicit override
2. `WORKSPACE_BASE` env (legacy `CLEANING_WORKSPACE_BASE` still honored)
3. `workspace.base_dir` in `config.yaml` (default `data/workspaces`, relative to the repository root; legacy `cleaning_workspace` key still honored)

`WorkspaceManager.archive_to_storage()` saves the workspace to the configured storage backend. `restore_from_storage()` downloads it again when a session resumes with an empty local directory, so workspaces survive container restarts.

---

## Configuration

| Setting | Where | Default | Description |
|---|---|---|---|
| `WORKSPACE_BASE` | env | — | Absolute base directory for workspaces |
| `workspace.base_dir` | `config.yaml` | `data/workspaces` | Base directory (lower precedence than env) |
| `cleaning.react_max_steps` | `config.yaml` | `50` | Maximum loop steps per query |

---

## API Endpoints

### `POST /agents/cleaning/quality`

Quick quality analysis of a posted dataset (dict-of-lists): duplicate counts, missing-value percentages, and numeric outliers, with LLM recommendations. Does not use the ReAct loop.

### `GET /agents/cleaning/health`

Readiness probe: `200` when the LLM router and cleaning tools are available, `503` otherwise. Unauthenticated.

---

## Export

`CleaningExporter` generates a human-readable `README.md` for exported cleaning artifacts:

- `generate_export_readme(code, plan, choices, dataset_summary)` — Python script exports
- `generate_export_readme_sql(sql, plan, choices, dataset_summary)` — SQL script exports

The export endpoints in `backend/api/routes.py` call these (`/api/export/cleaning-script/{session_id}`, `/api/export/cleaning-sql/{session_id}`).

# Unified Source Model

Status: **in progress** (Phase 1 landed). Owner: data-preview / agents.

## Why

Today the backend treats one dataset as special: an in-memory `working_dataframe`
(the "active dataset") that sub-agents are wired to. When an agent writes a new
file (a cleaned or feature-engineered spreadsheet), the orchestrator can't see it
and the next sub-agent still operates on the original frame. That blocks genuine
multi-file pipelines.

We are moving to a **file-first harness**: the workspace filesystem (plus any
connected SQL source) is the source of truth, no single file gets special
treatment, and sub-agents work on whatever source the orchestrator names.

## Decisions

- **SQL stays first-class (Option B — virtual source), not materialized to a
  file.** Materializing a DB table to parquet would cap size and throw away SQL
  pushdown / live re-query — i.e. *less* functionality over the user's database.
  Instead the SQL source is a *handle* (connection + `stych_temp` table) that
  appears in the source catalog like any file; sub-agents route by kind
  (file → pandas/sandbox, SQL → `run_sql`).
- **`working_dataframe` is demoted to a derived preview cache**, not an identity
  or a compute surface. Source of truth = files + SQL handles. It is computed on
  demand (`SELECT … LIMIT N`) and used only for the UI grid and LLM schema/sample
  context.

## Phases

**Phase 1 — Source catalog + orchestrator visibility (landed).**
- `workspace/sources.py: list_sources(state, root)` — one uniform list of every
  input: workspace data files (mirror copies collapsed) + the SQL source, each
  with name / kind / rows / columns / active flag.
- Orchestrator env block lists the catalog, so it sees agent-created files.
- `call_agent` gains an `input` arg; the orchestrator names the source a
  sub-agent should work on. It is threaded into the sub-agent prompt, so
  sub-agents (which already have `list_files`/`read_file`/`run_script`) act on it.

**Phase 2 — Deterministic source direction for sub-agents (landed).**
- When the orchestrator names a source via `call_agent input`, a *file* source is
  promoted to the active dataset (`set_active_dataset`) before dispatch, so every
  sub-agent — all of which read `state.data_context.dataframe` — operates on it,
  rather than relying on the prompt hint alone. SQL handles and unknown ids fall
  back to the prompt directive. See `_activate_named_source`.
- This keeps the frame but makes it *follow the orchestrator's direction* instead
  of being a fixed "original upload forever" identity — a step toward the cache.

**Phase 3a — de-privilege the frame in agent context (landed).**
- `list_sources` is now the *complete* catalog: in-memory datasets ∪ workspace
  files ∪ SQL. Additional (multi-upload) datasets aren't seeded to disk, so they
  are listed as `dataset:` sources — the catalog no longer hides them.
- `_env_block` dropped the privileged "working frame (primary/…)" lines entirely;
  the orchestrator now sees one uniform catalog with an `[active]` marker and
  per-source schemas. No single file gets special context. Linking still sees
  additional datasets' columns (they're in the catalog).

**Phase 3b (preview half) — DB preview served by pushdown (landed).**
The preview surface no longer reads `ctx.dataframe` for DB sources:
- `datasources/adapters/` — `TableAdapter` pushdown layer (Postgres / SQL
  Server / Mongo): schema, exact-or-estimated counts (8s `SET LOCAL
  statement_timeout`, `approx` flag on fallback), and full-table
  page/filter/search/sort executed inside the client's database. Postgres
  reuses the SQL agent's engine pool.
- `/preview/sources` lists DB contexts as `kind:"sql"` entries: the base table
  (`{file_id}::base`) and, once the agent creates it, the `stych_temp` working
  copy (`{file_id}::temp`, active). `/preview/page` accepts `source=sql`.
- `api/sql_diff_service.py` — SQL-side keyed diff (base vs working copy) in
  the client's Postgres: FULL OUTER JOIN summary aggregates, per-page cell
  old/new, transform classification via the pandas detectors on a 10K aligned
  sample; no key → degraded summary-only diff. Same `PreviewDiffPage` shape.
- Cache invalidation: `temp_table_generation` in ctx metadata, bumped by the
  SQL cleaning paths, keys the count/schema/diff caches.
- The 10K connect-time frame remains only as LLM schema/sample context and as
  the fallback when restored credentials are redacted.

**Phase 3b (compute half) — frame → preview cache (still deferred).**
The frame is still the compute surface for file-based sub-agents:
- Replace the persistent frame with a small LRU keyed by source
  (`preview_cache[source] -> df`, lazy, `LIMIT N`).
- Snapshot / resume persists the *sources* (files + connection meta), not each
  `working_dataframe` (today: `state.py` writes `{file_id}.parquet` + manifest and
  restores it on session load — a bug here silently loses a user's working state).
- Q&A / linking / graph load from source on demand.
- Converge `/preview/sources` listing with `workspace/sources.py list_sources`
  (deferred deliberately: one serves the UI, the other the orchestrator prompt).

Rationale for deferring: the frame is still the legitimate *compute surface* for
sub-agents and underpins snapshot/resume and raw-vs-working (undo) semantics.
Fully collapsing it to a preview cache only clearly pays off with the DuckDB north
star (compute becomes SQL over relations, so no frame is needed). Until then this
is a high-risk rewrite that must be verified against a live Postgres session
round-trip, not unit tests alone.

## North star (not yet built): one query engine over everything

The end state collapses the file/SQL dichotomy entirely with **DuckDB as a
universal engine**:

- A file is `SELECT * FROM 'workspace/data.parquet'`.
- A Postgres table is queried via DuckDB's `postgres` scanner
  (`SELECT * FROM pg.public.orders`) with pushdown to Postgres.
- Sub-agents write **one SQL dialect over both**, and can join an uploaded CSV
  against a live DB table with no materialization.

Benefits: keeps SQL pushdown/scale, makes files first-class SQL citizens, removes
`is_sql_datasource()` branching, and lets `working_dataframe` disappear (a preview
is just `LIMIT N` on a registered relation).

Caveats to design through before adopting: DuckDB's Postgres pushdown is
imperfect (some ops pull rows), write-back to Postgres is not its strength (we are
mostly read + write-outputs-as-files, so acceptable), and it adds an engine to the
sandbox. It is the destination, staged after Phase 2's source-catalog abstraction
exists — that abstraction is exactly what DuckDB's catalog would register.

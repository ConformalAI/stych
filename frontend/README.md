# Stych Frontend

React 18 + TypeScript UI for the Stych data agent platform.

## Quick Start

```bash
npm install
cp .env.example .env
npm run dev            # http://localhost:5173
```

Requires Node ≥ 20 and npm ≥ 10.

## Tech Stack

| Package | Purpose |
|---|---|
| React 18 + TypeScript | UI framework |
| Vite 6 | Build tool + dev server (proxies `/api` and `/auth`) |
| Tailwind CSS 3.4 | Styling |
| Zustand | Global state (`useStore`, `useAuthStore`) |
| TanStack Query | Server-state caching |
| ECharts | Visualize tab and chat `VizCard` rendering |
| Recharts | Legacy ```` ```chart ```` / ```` ```charts ```` fences in chat |
| Vega / Vega-Lite | Linking inspect views and ```` ```vega ```` fences |
| `@xyflow/react` | Ontology canvas |
| `@dnd-kit` | Drag-and-drop encoding shelves |
| `react-markdown` + `remark-gfm` | Markdown + tables in chat |
| Axios / native `fetch` | REST calls / SSE streaming |
| React Router v6 | Routing |
| supertokens-auth-react | Auth (EmailPassword + Google ThirdParty, header-token mode) |
| Vitest + jsdom | Tests |

`@react-oauth/google` is still listed in `package.json` but is not imported; Google sign-in goes through SuperTokens.

## Project Structure

```
src/
├── App.tsx                 # routes, auth polling, ProtectedRoute
├── main.tsx                # SuperTokens init, ErrorBoundary
├── pages/
│   ├── Landing.tsx         # public landing + first-question composer
│   ├── Login.tsx           # email/password, Google, optional dev login
│   ├── AuthCallback.tsx    # /oauth/callback/google
│   ├── Chat.tsx            # agent workspace: Chat · Data · Visualize · Ontology
│   ├── Connectors.tsx      # saved connections library
│   └── Settings.tsx        # profile, usage, models, memory, integrations
├── components/
│   ├── shell/              # AppShell, PrimaryRail, navItems
│   ├── ChatMessages.tsx    # messages, markdown, action prompts, inline viz/ontology cards
│   ├── AgentTrace.tsx      # live + persisted trace parts (uses StepItem from StepTimeline.tsx)
│   ├── ChatInput.tsx       # input, Stop/Send, SlashCommandMenu
│   ├── Sidebar.tsx         # session list with titles
│   ├── visualize/          # VizView, EncodingShelves, EChartsRenderer, VizCard, AskThisChart, …
│   ├── preview/            # DataPreviewView (browse / diff / filter), SpreadsheetGrid, SourcePicker
│   ├── ontology/           # OntologyView, OntologyCanvas, Inspector, VersionHistory
│   ├── connectors/         # cards, filter rail, detail drawer, tags, namespaces, forms
│   ├── data/AddDataModal.tsx  # upload or attach from saved connections
│   ├── WarehouseConnect*.tsx, PostgresConnect, SqlServerConnect, MongoConnect
│   └── Header, Toast, ErrorBoundary, MemoryManager, IntegrationSelector, …
├── hooks/
│   ├── useChat.ts          # create runs, attach/reconnect, retry, cancel
│   └── agentRunStream.ts   # SSE consumer + exhaustive event handling
├── services/               # api, auth, authHeaders, viz, filters, ontology, sse, storage
├── store/                  # useStore.ts, useAuthStore.ts
└── generated/              # schema/*.json exported from backend + contracts/*.ts (generated)
```

## Routing

| Path | Page | Notes |
|---|---|---|
| `/` | Landing | Public |
| `/login` | Login | |
| `/oauth/callback/google` | AuthCallback | Kept outside `/auth` so the Vite proxy doesn't catch it |
| `/chat` | Chat | Inside `AppShell` (header + primary rail) |
| `/connectors` | Connectors | Inside `AppShell` |
| `/settings` | Settings | Protected, rendered outside `AppShell` |

## Key Behaviours

### Agent runs and streaming

1. `useChat` sends `POST /api/query/runs` with `session_id`, `message` (or `__user_action__`), `task_type`, model, enabled integrations, `client_request_id`, and an optional `target_file_id`.
2. It attaches to `GET /api/query/runs/{id}/stream`. A failed attach is retried up to 3 times with exponential backoff (1 s, 2 s).
3. On a 409 (the session already has an active run), it attaches to the latest run instead.
4. When you open or switch to a session, `GET /api/query/runs/session/{id}` re-attaches to any queued or running run; otherwise history loads from `/api/history/{id}`.
5. Runs belong to the backend process, so leaving the page only detaches the viewer.

`agentRunStream.ts` handles each event exhaustively against the generated `SseEventType` union:

| Event | Action |
|---|---|
| `run_snapshot` | Restore streaming text, trace parts, artifact events, and terminal state |
| `thinking` | Live working indicator |
| `part.updated` | Upsert a trace part in the live trace |
| `token` / `thinking_token` | Append answer text / model reasoning |
| `token_retract` | Clear the streaming bubble |
| `compaction_start` / `compaction_end` | "Chat compacted" notice |
| `viz` / `filter` / `ontology` | Refresh or badge the Visualize / Data / Ontology tab |
| `result` | Commit the assistant message (trace parts + thinking go into metadata) |
| `error` / `timeout` / `limit_reached` / `cancelled` | Terminal commit (no error bubble for cancel) |
| `heartbeat` / `reasoning` | Ignored |

### Stop / Cancel

Stop calls `POST /api/query/runs/{id}/cancel`, then detaches the viewer and clears live state.

### Workspace tabs

- **Chat** — conversation, action prompts (HITL questions and approvals), inline chart and ontology cards.
- **Data** — paged grid of any source (files, base tables, working copies), diffs between sources, saved filters, downloads.
- **Visualize** — edit `VizSpec`s with encoding shelves, filters, calculated fields, styles and version history; "Ask this chart" sends a selection back to chat.
- **Ontology** — entities, relationships, dimensions and metrics; accept or reject agent proposals; certification and versions.

Tabs show a badge when an agent creates an artifact while you're on another tab.

### Connectors

The Connectors page manages saved, per-user connections (Postgres, SQL Server, MongoDB, Snowflake, Redshift, BigQuery, Databricks). You can test them, tag them, browse namespaces and tables, pin tables, and delete them (with a usage check). The **Add data** dialog in Chat attaches a saved connection or pinned table to the session; it has no credential forms.

### Auth

SuperTokens runs in header-token mode, and `services/authHeaders.ts` adds `st-access-token` to Axios and SSE requests. A 401 redirects to `/login`, except on public routes. `App.tsx` checks the session every 15 s and on window focus. `ProtectedRoute` re-validates `/api/auth/me` every 30 s and on focus.

## State Management (Zustand)

Key slices in `useStore.ts`:

| Slice | Purpose |
|---|---|
| `sessionId`, `isAnonymousSession` | Current session |
| `messages[]`, `resolvedActionPromptIds` | Conversation and answered prompts |
| `uploadedFiles[]` (read the primary file with `usePrimaryFile`) | Datasets attached to the session |
| `thinkingAgent`, `liveTraceParts`, streaming answer / thinking text, `isCompacting` | Live run UI |
| `lastVizEvent`, `lastFilterEvent`, `lastOntologyEvent`, `openVizId`, `openFilterId`, `openOntologyName`, badges, `activeView` | Cross-tab artifact signals |
| `sessionModel`, `selectedModel`, `availableModels` | Model selection |
| `enabledIntegrations`, `pendingQuery`, `chatTheme`, toasts | Misc UI |

`resetSession` clears all live state. Persistent browser state (session id, drafts, theme, rail state) goes through `services/storage.ts`.

## Generated Contracts

`src/generated/schema/*.json` is exported from the backend by `backend/scripts/export_contract_schemas.py`, and `scripts/codegen-contracts.cjs` generates `src/generated/contracts/*.ts` from it (VizSpec, SpecError, FieldCatalog, viz literals, SSE event types, task types). Don't edit either folder by hand.

## Environment Variables

```bash
VITE_API_BASE_URL=http://localhost:8000          # optional; defaults to same origin
VITE_PROXY_TARGET=http://localhost:8000          # Vite dev proxy target for /api and /auth
VITE_SUPERTOKENS_API_DOMAIN=http://localhost:5173
VITE_SUPERTOKENS_WEBSITE_DOMAIN=http://localhost:5173
VITE_ALLOWED_HOSTS=localhost                      # comma-separated dev-server host allowlist
VITE_DEV_MODE=false                               # show "Dev Login"; requires backend DEV_MODE=true
VITE_HINDSIGHT_WEB_UI_URL=http://localhost:9999   # optional Hindsight admin UI link
```

## Commands

```bash
npm run dev            # development server
npm run build          # type-check + production build → dist/
npm run preview        # preview production build
npm run lint           # ESLint
npm run lint:fix       # ESLint auto-fix
npm run format         # Prettier write
npm run format:check   # Prettier check
npm run type-check     # tsc --noEmit
npm test               # Vitest (runs codegen:check first)
npm run codegen        # re-export backend schemas + regenerate TS contracts
npm run codegen:check  # fail if generated contracts are out of date
npm run check          # type-check + lint + format:check + test
npm run clean:cache    # wipe Vite / ESLint caches
```

## Deployment

`Dockerfile` builds static assets and serves them with nginx (`nginx.conf`: SPA fallback, SSE buffering disabled for `/api/query`). `Dockerfile.dev` runs the Vite dev server on port 5173.

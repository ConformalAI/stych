# Development Setup Guide

For installation and environment variable reference see [INSTALL.md](INSTALL.md).  
For architecture details see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Day-to-Day Development

### Starting the Servers

```bash
# Backend (auto-reload on file change)
cd backend
source .venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Frontend (HMR)
cd frontend
npm run dev
```

| URL | Purpose |
|---|---|
| http://localhost:5173 | React app |
| http://localhost:8000 | FastAPI backend |
| http://localhost:8000/docs | Swagger / OpenAPI |
| http://localhost:8000/redoc | ReDoc |

---

## Code Quality

### Backend

All tools live in the `.venv`; no global installs needed.

```bash
cd backend

# Lint (ruff — fast, pyflakes + isort + pycodestyle rules)
.venv/bin/ruff check .

# Auto-fix lint issues (import sorting etc.)
.venv/bin/ruff check . --fix

# Format (black, line-length 100)
.venv/bin/black --line-length 100 .

# Type check
.venv/bin/mypy agents/

# Remove compiled bytecode / __pycache__
.venv/bin/pyclean . --debris --yes
```

**Ruff configuration** (`pyproject.toml → [tool.ruff.lint]`):
- Enabled rule sets: `E`, `F`, `W`, `I` (pycodestyle errors, pyflakes, warnings, isort)
- `E501` (line length) is ignored — black owns that
- `E402` is ignored for `main.py` — dotenv must load before local imports

### Frontend

```bash
cd frontend

# ESLint
npm run lint            # report errors + warnings
npm run lint:fix        # auto-fix what ESLint can
npm run lint:strict     # fail on any warning (CI mode)

# Prettier
npm run format          # reformat all files in src/
npm run format:check    # check without writing (CI mode)

# TypeScript
npm run type-check      # tsc --noEmit, no output files

# Run all three checks in sequence
npm run check

# Cache cleaning
npm run clean:cache     # remove node_modules/.vite and node_modules/.cache
npm run clean:dist      # remove dist/
npm run clean           # both of the above
```

**ESLint config** (`.eslintrc.json`):
- Extends `eslint:recommended`, `@typescript-eslint/recommended`, `react-hooks/recommended`, `prettier`
- `@typescript-eslint/no-unused-vars` — errors, but ignores `_`-prefixed names (args, vars, catch vars)
- `react-hooks/exhaustive-deps` — warn
- `no-console` — warn (allows `console.error` and `console.warn`)

**Prettier config** (`.prettierrc.json`): `singleQuote`, `semi`, `trailingComma: es5`, `tabWidth: 4`, `printWidth: 100`.

---

## Pre-commit Hook

A git hook runs automatically before every `git commit`. It only executes the backend checks if `backend/` files are staged, and only frontend checks if `frontend/` files are staged.

### Installing

```bash
# From repo root — run once after cloning
bash scripts/install-hooks.sh
```

This copies `scripts/hooks/pre-commit` into `.git/hooks/pre-commit`. The source hook in `scripts/hooks/` is tracked by git and shared across the team.

### What It Runs

**Backend** (when `backend/` files are staged):

1. `pyclean` — removes `.pyc` and `__pycache__` (warning only if unavailable)
2. `ruff check` — blocks commit on any lint error
3. `black --check` — blocks commit if formatting differs

**Frontend** (when `frontend/` files are staged):

1. Cache clean — removes `node_modules/.vite` and `node_modules/.cache`
2. `eslint` — blocks commit on any error
3. `prettier --check` — blocks commit if formatting differs

### Bypassing (emergency only)

```bash
git commit --no-verify -m "message"
```

---

## Database

```bash
cd backend

# Apply all pending migrations
.venv/bin/alembic upgrade head

# Generate a new migration after changing SQLAlchemy models
.venv/bin/alembic revision --autogenerate -m "add my_column to users"

# Roll back one step
.venv/bin/alembic downgrade -1

# Show applied revisions
.venv/bin/alembic history --verbose
```

Migration files live in `backend/alembic/versions/`. Always review auto-generated migrations before committing.

---

## Testing

```bash
cd backend

# Run all tests
.venv/bin/pytest tests/ -v

# Run a single file
.venv/bin/pytest tests/test_agents.py -v

# With coverage
.venv/bin/pytest tests/ --cov=agents --cov-report=html

# Quick agent smoke test (no pytest)
.venv/bin/python test_agents.py
```

---

## Adding a New Agent

1. **Create the package:**

```bash
mkdir -p backend/agents/myagent
touch backend/agents/myagent/{__init__,agent,routes}.py
```

2. **Implement the agent** (`agent.py`):

```python
from ..base.agent import BaseStychAgent
from llm.infer import LLMRouter

class MyAgent(BaseStychAgent):
    def __init__(self, llm_router: LLMRouter):
        super().__init__(name="MyAgent", description="...", llm_router=llm_router)

    async def process_query(self, state, message, **kwargs):
        # call self.llm_router.infer(...) and return a dict
        ...
```

3. **Register in orchestrator** (`agents/orchestrator/agent.py`):

```python
from ..myagent.agent import MyAgent

class OrchestratorAgent:
    def __init__(self, llm_router):
        ...
        self.my_agent = MyAgent(llm_router)
```

4. **Add intent routing** in `OrchestratorAgent._detect_intent`:

```python
if "my_keyword" in message_lower:
    return "my_intent"
```

5. **Include the router** in `main.py`:

```python
from agents.myagent.routes import router as myagent_router
app.include_router(myagent_router)
```

6. **Add `task_type` to schema** (`api/schemas.py`) if the agent exposes a new task type:

```python
task_type: Optional[str] = Field(
    default="auto",
    pattern="^(auto|analysis|cleaning|qa|linking|my_intent)$",
)
```

---

## Adding a Frontend Component

1. Create `frontend/src/components/MyComponent.tsx`
2. Implement with strict TypeScript — no `any` without a comment explaining why
3. Run `npm run lint` and `npm run format` before committing
4. Import in the relevant page under `src/pages/`

---

## Debugging

### Backend

```bash
# Structured logging is already set up via Python logging module.
# Add targeted log lines:
import logging
logger = logging.getLogger(__name__)
logger.debug("state: %s", state)
```

VSCode launch config (`.vscode/launch.json`):

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "FastAPI",
      "type": "python",
      "request": "launch",
      "module": "uvicorn",
      "args": ["main:app", "--reload", "--port", "8000"],
      "cwd": "${workspaceFolder}/backend",
      "justMyCode": false
    }
  ]
}
```

### Frontend

- Browser DevTools → Network tab: watch SSE chunks on `/api/query`
- React Query DevTools (enabled in dev): inspect cache state
- Zustand — `window.__ZUSTAND__` exposes the store in dev builds

---

## Commit Convention

```
feat: add linking agent fuzzy matching strategy
fix: restore snapshot before first query after server restart
refactor: extract storage abstraction into helpers/storage.py
docs: update ARCHITECTURE with session snapshot flow
chore: bump ruff to 0.15, fix new I001 warnings
test: add upload endpoint integration test
```

---

## Git Workflow

```bash
# Create a feature branch
git checkout -b feat/my-feature

# Make changes, run checks
cd backend && .venv/bin/ruff check . && .venv/bin/black --check .
cd frontend && npm run check

# Commit (pre-commit hook runs automatically)
git add .
git commit -m "feat: my feature"

# Push and open PR
git push origin feat/my-feature
```

---

## Useful References

- FastAPI: https://fastapi.tiangolo.com/
- SQLAlchemy 2: https://docs.sqlalchemy.org/en/20/
- Alembic: https://alembic.sqlalchemy.org/
- SuperTokens Python SDK: https://supertokens.com/docs/python/
- LiteLLM: https://docs.litellm.ai/
- Ruff: https://docs.astral.sh/ruff/
- React Query: https://tanstack.com/query/latest
- ECharts: https://echarts.apache.org/
- Recharts (legacy chat charts): https://recharts.org/
- Vega-Lite: https://vega.github.io/vega-lite/
- DuckDB: https://duckdb.org/docs/

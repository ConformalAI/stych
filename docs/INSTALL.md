# Stych Installation Guide

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| Node.js | 20+ | |
| npm | 10+ | |
| PostgreSQL | 14+ | app database (auth, sessions, files) |
| MinIO **or** AWS S3 | any | object storage for uploads + snapshots |
| SuperTokens Core | latest | session management |
| LiteLLM / OpenRouter / Google AI | — | at least one LLM provider API key |

Docker Compose stacks for PostgreSQL, MinIO, SuperTokens, and Langfuse are in `deployment/`.

---

## Quick Start (Local Development)

### 1. Backend

```bash
cd backend

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

# Install with dev dependencies
pip install -e ".[dev]"

# Copy and fill in the environment file
cp .env.example .env               # edit .env — see "Environment Variables" below

# Run database migrations
alembic upgrade head

# Start the server
python main.py                     # or: uvicorn main:app --host 0.0.0.0 --port 8000
```

API available at **http://localhost:8000** · Swagger at **http://localhost:8000/docs**

### 2. Frontend

```bash
cd frontend

npm install

# (optional) install the pre-commit hook
bash ../scripts/install-hooks.sh

npm run dev
```

Frontend available at **http://localhost:5173**

---

## Environment Variables

All configuration lives in `backend/.env`. The file overrides `config/config.yaml` at startup.  
Copy `.env.example` and fill in the values for your environment.

### Server

```env
ENV=development          # development | production
HOST=0.0.0.0
PORT=8000
DEBUG=true               # false in production
LOG_LEVEL=INFO
LOG_FILE=./logs/stych.log
CORS_ORIGINS=http://localhost:3000,http://localhost:5173
```

### Dev Mode

```env
# true = bypasses SuperTokens auth; registers POST /api/auth/dev for local testing
# NEVER set to true in production
DEV_MODE=true
INVALIDATE_SESSIONS_ON_RESTART=true
```

### LLM Provider

Choose one primary provider. LiteLLM is the recommended default (can proxy to any model).

**Option A — LiteLLM (default)**
```env
LITELLM_BASE_URL=http://localhost:4000     # your LiteLLM gateway
LITELLM_API_KEY=sk-...
LITELLM_DEFAULT_MODEL=openrouter/anthropic/claude-sonnet-4.5
```

**Option B — OpenRouter**
```env
# Set llm.provider=openrouter in config.yaml, then:
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_DEFAULT_MODEL=anthropic/claude-3.5-sonnet
```

**Option C — Google AI**
```env
# Set llm.provider=google in config.yaml, then:
GOOGLE_API_KEY=AIza...
GOOGLE_PROJECT_ID=your-project-id
```

### App Database (PostgreSQL)

```env
APP_DATABASE_URL=postgresql://user:password@host:5432/stych_db
APP_DB_SCHEMA=public
APP_DB_REQUIRE_POSTGRES=true

# Backwards-compat alias (same value)
DATABASE_URL=postgresql://user:password@host:5432/stych_db
```

Run migrations after setting `APP_DATABASE_URL`:
```bash
cd backend && alembic upgrade head
```

### Object Storage

**Option A — Local disk (dev / single-node)**
```env
STORAGE_BACKEND=local
UPLOAD_DIR=./data/uploads
TEMP_DIR=./data/temp
SNAPSHOT_DIR=./data/snapshots
```

**Option B — MinIO**
```env
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=http://localhost:9100   # MinIO S3 API port (not the console port)
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_BUCKET=stych-uploads
S3_REGION=us-east-1
S3_PATH_STYLE=true                      # required for MinIO
```

**Option C — AWS S3**
```env
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=                        # leave empty for AWS
S3_ACCESS_KEY=AKIA...
S3_SECRET_KEY=...
S3_BUCKET=your-bucket-name
S3_REGION=us-east-1
S3_PATH_STYLE=false
```

> **MinIO ports** — the MinIO Docker image exposes two ports: the S3 API (default 9000, mapped to 9100 in the deployment stack) and the console UI (default 9001, mapped to 9101). Use the **S3 API port** for `S3_ENDPOINT_URL`.

### Authentication (SuperTokens)

```env
SUPERTOKENS_CONNECTION_URI=http://localhost:3567
SUPERTOKENS_API_KEY=your-supertokens-api-key

# Domains — must match exactly what the browser sees
API_DOMAIN=http://localhost:8000
WEBSITE_DOMAIN=http://localhost:5173

# Google OAuth (optional)
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

### Security

```env
SECRET_KEY=change-this-in-production-minimum-32-chars
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
```

### Observability (Langfuse — optional)

```env
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3110    # your Langfuse instance
```

### File Upload Limits

```env
UPLOAD_MAX_SIZE_MB=50
```

### A2A Protocol (optional)

```env
ENABLE_A2A=true
A2A_SERVER_HOST=0.0.0.0
A2A_SERVER_PORT=8001
A2A_AGENT_ID=stych-data-agent
A2A_AGENT_NAME=Stych Data Analysis Agent
```

### Admin Access

```env
# Comma-separated list of email addresses with admin privileges
ADMIN_EMAILS=admin@yourcompany.com

# Or by local user ID
ADMIN_USER_IDS=
```

---

## Infrastructure Services

The `deployment/` directory has Docker Compose stacks for supporting services.

### Starting Services

```bash
# PostgreSQL (app database)
cd deployment/postgres
docker compose up -d

# MinIO (object storage)
cd deployment/storage
docker compose up -d

# SuperTokens Core (auth)
cd deployment/supertokens
docker compose up -d

# Langfuse (LLM observability — optional)
cd deployment/langfuse
docker compose up -d
```

Each stack has its own `.env` file with service credentials. The MinIO deployment `.env` contains the `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` that must match `S3_ACCESS_KEY` and `S3_SECRET_KEY` in `backend/.env`.

### Default Ports (deployment stack)

| Service | Port | Notes |
|---|---|---|
| PostgreSQL | 5433 | mapped from container 5432 |
| MinIO S3 API | 9100 | mapped from container 9000 — use this in `S3_ENDPOINT_URL` |
| MinIO Console | 9101 | browser UI only — do not use in `S3_ENDPOINT_URL` |
| SuperTokens Core | 3567 | |
| Langfuse | 3110 | |
| LiteLLM Gateway | 4000 | |

---

## Database Migrations

```bash
cd backend

# Apply all pending migrations
alembic upgrade head

# Create a new migration after changing models
alembic revision --autogenerate -m "describe your change"

# Roll back one migration
alembic downgrade -1

# Show current revision
alembic current
```

---

## Verifying the Installation

### 1. Health check

```bash
curl http://localhost:8000/api/health
```

Expected:
```json
{
  "status": "healthy",
  "uptime": 12.4,
  "db": "ok",
  "storage": "ok"
}
```

### 2. Upload a file

```bash
# Requires a valid session cookie. In DEV_MODE, get a dev token first:
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/dev \
  -H "Content-Type: application/json" \
  -d '{"email":"dev@stych.local"}' | jq -r '.access_token')

# Upload
curl -X POST http://localhost:8000/api/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sample_data/customers_sample.csv"
```

### 3. Send a query

```bash
SESSION_ID="<session_id from upload response>"

curl -X POST http://localhost:8000/api/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"message\": \"Describe this dataset\"}"
```

---

## Troubleshooting

### `RuntimeError: Missing app database URL`

`APP_DATABASE_URL` is not set or `.env` is not in `backend/`. Ensure `.env` exists and contains `APP_DATABASE_URL`.

### `S3 API Requests must be made to API port`

`S3_ENDPOINT_URL` is pointing at the MinIO console port (9101), not the S3 API port (9100). Check `deployment/storage/.env` for the correct mapping.

### `An error occurred (InvalidArgument) when calling PutObject`

Usually a credentials mismatch. Verify `S3_ACCESS_KEY` and `S3_SECRET_KEY` match `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` in `deployment/storage/.env`.

### Environment variables not taking effect

`load_dotenv` is called with `override=True` in `main.py`, so `.env` always wins. If you previously exported the variable in your shell (`export STORAGE_BACKEND=s3`), clear it with `unset STORAGE_BACKEND` before starting the server.

### `alembic: command not found`

Use the venv: `cd backend && .venv/bin/alembic upgrade head`

### Frontend won't start

```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
npm run dev
```

### Port already in use

```bash
lsof -ti:8000 | xargs kill -9   # backend
lsof -ti:5173 | xargs kill -9   # frontend
```

---

## Production Checklist

- [ ] `DEV_MODE=false` — never bypass auth in production
- [ ] `SECRET_KEY` — long random string, never commit to git
- [ ] `DEBUG=false`
- [ ] `STORAGE_BACKEND=s3` with a durable bucket
- [ ] SSL/TLS termination in front of FastAPI
- [ ] `CORS_ORIGINS` set to actual production domain(s)
- [ ] `API_DOMAIN` and `WEBSITE_DOMAIN` set to production URLs
- [ ] `alembic upgrade head` run before starting the new version
- [ ] Database backups configured
- [ ] Langfuse configured for production observability
- [ ] `ADMIN_EMAILS` set for at least one operator account

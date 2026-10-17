# Authentication System - SuperTokens Sessions

## Overview

Stych uses **SuperTokens** for authentication and session management.

- Primary auth is **session-cookie based** (no custom JWT middleware in request flow).
- Local `users` table remains the application identity store.
- `supertokens_id` is the bridge between SuperTokens users and local users.

## Runtime Flow

1. Frontend initializes SuperTokens (`frontend/src/main.tsx`).
2. User signs in through SuperTokens-managed routes under `/auth/*`.
3. SuperTokens creates/validates session cookies.
4. Backend `get_current_user` verifies session via `verify_session()`.
5. Backend resolves local `User` row by `supertokens_id`.

## Backend Components

- `backend/auth/supertokens_config.py`
  - Initializes SuperTokens SDK and recipes.
  - Syncs SuperTokens users into local `users` table.
- `backend/api/dependencies.py`
  - `get_current_user_record`: authenticated local user lookup.
  - `get_current_user`: returns local user ID.
  - `get_current_admin_user`: enforces admin-only access.
- `backend/api/auth_routes.py`
  - App-level auth endpoints (e.g. profile and dev mode helpers).

## Admin Authorization

Privileged endpoints use `get_current_admin_user`, with admin grants from:

- `ADMIN_EMAILS` (comma-separated email allowlist)
- `ADMIN_USER_IDS` (comma-separated local user ID allowlist)
- optional `user.is_admin` field if introduced in DB schema later

## Required Env Vars

- `SUPERTOKENS_CONNECTION_URI`
- `API_DOMAIN`
- `WEBSITE_DOMAIN`

Optional:

- `SUPERTOKENS_API_KEY`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `ADMIN_EMAILS`
- `ADMIN_USER_IDS`

## Dev Mode

If `DEV_MODE=true`, backend registers `POST /api/auth/dev` for local development bootstrap.  
In non-dev mode, this route is not registered.

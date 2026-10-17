# Contributing to Stych

## Before you start

1. **Licensing.** Stych is licensed under the AGPL-3.0-only, and we plan to move the open core to a more permissive license later. See [`LICENSING.md`](LICENSING.md).
2. **Sign the CLA.** Every contributor must agree to the [CLA](CLA.md) before their work can be merged. Comment this sentence on your first pull request:

   > I have read the CLA Document and I hereby sign the CLA.

3. **Open an issue first for large changes**, such as a new connector or a change to the agent engine.

## Development setup

See [`docs/INSTALL.md`](docs/INSTALL.md) and [`docs/SETUP.md`](docs/SETUP.md). You need Python 3.11+, Node 20+, PostgreSQL, SuperTokens Core, and an LLM endpoint.

## Making a change

1. Fork the repository and branch from `dev`.
2. Keep the change focused, and add or update tests.
3. Run the checks:
   - Backend, from `backend/`: `ruff check .`, `black --line-length 100 .`, `pytest`.
   - Frontend, from `frontend/`: `npm run check`. If you change a backend contract (VizSpec, SSE events, task types), run `npm run codegen` and commit the generated files.
4. Open a pull request against `dev` describing what changed and why.

## Rules

- Never commit secrets, credentials, or data you are not allowed to redistribute.
- Only submit code you have the right to submit. If you include third-party code, note its source and license, and make sure the license is compatible with the AGPL-3.0.
- Stych never writes to a user's source tables. Changes to `write_guard.py`, `pipeline_guard.py`, the sandbox, or credential handling need tests.

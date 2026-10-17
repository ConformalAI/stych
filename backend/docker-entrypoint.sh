#!/bin/sh
# Container entrypoint: bring the database schema up to date, then start the API.
# Skip the migration step with RUN_MIGRATIONS=false.
set -e

python "$(dirname "$0")/scripts/migrate.py"

exec "$@"

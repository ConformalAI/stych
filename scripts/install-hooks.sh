#!/usr/bin/env bash
# Run once after cloning to install the git pre-commit hook.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_SRC="$REPO_ROOT/scripts/hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

if [[ ! -d "$REPO_ROOT/.git" ]]; then
    echo "Error: must be run from inside the git repository." >&2
    exit 1
fi

# Copy the canonical hook from scripts/hooks/ into .git/hooks/
cp "$HOOKS_SRC/pre-commit" "$HOOKS_DST/pre-commit"
chmod +x "$HOOKS_DST/pre-commit"

echo "✓ pre-commit hook installed at $HOOKS_DST/pre-commit"

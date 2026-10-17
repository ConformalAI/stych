"""add description, tags, health and last-used metadata to user_connections

Revision ID: 20260818_0016
Revises: 20260816_0015
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260818_0016"
down_revision = "20260816_0015"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("description", sa.Text()),
    ("tags_json", sa.Text()),
    ("last_test_status", sa.String(16)),
    ("last_test_error", sa.Text()),
    ("last_used_at", sa.DateTime()),
)


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(op.get_bind())
    if "user_connections" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("user_connections")}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column("user_connections", sa.Column(name, type_, nullable=True))

    # Backfill: rows created before this migration were tested successfully at
    # creation time (create_connection only persists after test_and_normalize).
    op.execute(
        "UPDATE user_connections SET last_test_status = 'ok' "
        "WHERE last_test_status IS NULL AND last_tested_at IS NOT NULL"
    )


def downgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(op.get_bind())
    if "user_connections" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("user_connections")}
    for name, _type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("user_connections", name)

"""add long_term_memory_enabled to app_users for per-user memory opt-out

Revision ID: 20260527_0009
Revises: 20260522_0008
Create Date: 2026-05-27 00:00:00
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260527_0009"
down_revision: Union[str, None] = "20260522_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema() -> str:
    return os.getenv("APP_DB_SCHEMA", "public").strip() or "public"


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "app_users",
        sa.Column(
            "long_term_memory_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_column("app_users", "long_term_memory_enabled", schema=schema)

"""add preferred_model to app_users for per-user LLM override

Revision ID: 20260420_0007
Revises: 20260411_0006
Create Date: 2026-04-20 00:00:00
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260420_0007"
down_revision: Union[str, None] = "20260411_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema() -> str:
    return os.getenv("APP_DB_SCHEMA", "public").strip() or "public"


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "app_users",
        sa.Column("preferred_model", sa.String(255), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_column("app_users", "preferred_model", schema=schema)

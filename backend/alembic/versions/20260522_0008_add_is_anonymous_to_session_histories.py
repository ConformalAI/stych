"""add is_anonymous to session_histories for memory-free anonymous sessions

Revision ID: 20260522_0008
Revises: 20260420_0007
Create Date: 2026-05-22 00:00:00
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260522_0008"
down_revision: Union[str, None] = "20260420_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema() -> str:
    return os.getenv("APP_DB_SCHEMA", "public").strip() or "public"


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "session_histories",
        sa.Column(
            "is_anonymous",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_column("session_histories", "is_anonymous", schema=schema)

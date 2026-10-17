"""add uploaded_files table for storage-backend-agnostic file tracking

Revision ID: 20260411_0006
Revises: 20260331_0005
Create Date: 2026-04-11 00:00:00
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "20260411_0006"
down_revision: Union[str, None] = "20260331_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema() -> str:
    return os.getenv("APP_DB_SCHEMA", "public").strip() or "public"


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("app_user_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=True),
        sa.Column("storage_backend", sa.String(), nullable=False, server_default="local"),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["app_user_id"], [f"{schema}.app_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    op.create_index(
        "ix_uploaded_files_app_user_id",
        "uploaded_files",
        ["app_user_id"],
        schema=schema,
    )
    op.create_index(
        "ix_uploaded_files_session_id",
        "uploaded_files",
        ["session_id"],
        schema=schema,
    )
    op.create_index(
        "ix_uploaded_files_created_at",
        "uploaded_files",
        ["created_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_uploaded_files_created_at", table_name="uploaded_files", schema=schema)
    op.drop_index("ix_uploaded_files_session_id", table_name="uploaded_files", schema=schema)
    op.drop_index("ix_uploaded_files_app_user_id", table_name="uploaded_files", schema=schema)
    op.drop_table("uploaded_files", schema=schema)

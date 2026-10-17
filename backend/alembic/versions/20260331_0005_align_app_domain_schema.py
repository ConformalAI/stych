"""align app-domain schema with target architecture

Revision ID: 20260331_0005
Revises: 20260331_0004
Create Date: 2026-03-31 12:15:00
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260331_0005"
down_revision: Union[str, None] = "20260331_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema() -> str:
    return os.getenv("APP_DB_SCHEMA", "public").strip() or "public"


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    query = sa.text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
          AND column_name = :column
        LIMIT 1
        """)
    return (
        bind.execute(query, {"schema": schema, "table": table, "column": column}).first()
        is not None
    )


def upgrade() -> None:
    schema = _schema()

    # app_users
    if not _column_exists(schema, "app_users", "profile_picture"):
        op.add_column(
            "app_users",
            sa.Column("profile_picture", sa.String(), nullable=True),
            schema=schema,
        )

    # user_quotas
    if not _column_exists(schema, "user_quotas", "plan"):
        op.add_column("user_quotas", sa.Column("plan", sa.String(), nullable=True), schema=schema)
    if not _column_exists(schema, "user_quotas", "token_limit_daily"):
        op.add_column(
            "user_quotas",
            sa.Column("token_limit_daily", sa.Integer(), nullable=True),
            schema=schema,
        )
    op.execute(sa.text(f'UPDATE "{schema}"."user_quotas" SET "plan" = COALESCE("plan", \'free\')'))
    op.alter_column("user_quotas", "plan", existing_type=sa.String(), nullable=False, schema=schema)

    # session_histories
    if not _column_exists(schema, "session_histories", "started_at"):
        op.add_column(
            "session_histories",
            sa.Column("started_at", sa.DateTime(), nullable=True),
            schema=schema,
        )
        op.execute(
            sa.text(
                f'UPDATE "{schema}"."session_histories" SET "started_at" = COALESCE("created_at", NOW())'
            )
        )
        op.alter_column(
            "session_histories",
            "started_at",
            existing_type=sa.DateTime(),
            nullable=False,
            schema=schema,
        )
        op.create_index(
            op.f("ix_session_histories_started_at"),
            "session_histories",
            ["started_at"],
            unique=False,
            schema=schema,
        )
    if not _column_exists(schema, "session_histories", "last_activity_at"):
        op.add_column(
            "session_histories",
            sa.Column("last_activity_at", sa.DateTime(), nullable=True),
            schema=schema,
        )
        op.create_index(
            op.f("ix_session_histories_last_activity_at"),
            "session_histories",
            ["last_activity_at"],
            unique=False,
            schema=schema,
        )
    if not _column_exists(schema, "session_histories", "total_tokens"):
        op.add_column(
            "session_histories",
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            schema=schema,
        )
    if not _column_exists(schema, "session_histories", "total_credits"):
        op.add_column(
            "session_histories",
            sa.Column("total_credits", sa.Float(), nullable=False, server_default="0"),
            schema=schema,
        )

    # session_messages
    if not _column_exists(schema, "session_messages", "metadata_json"):
        op.add_column(
            "session_messages",
            sa.Column("metadata_json", sa.Text(), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    schema = _schema()

    if _column_exists(schema, "session_messages", "metadata_json"):
        op.drop_column("session_messages", "metadata_json", schema=schema)

    if _column_exists(schema, "session_histories", "total_credits"):
        op.drop_column("session_histories", "total_credits", schema=schema)
    if _column_exists(schema, "session_histories", "total_tokens"):
        op.drop_column("session_histories", "total_tokens", schema=schema)
    if _column_exists(schema, "session_histories", "last_activity_at"):
        op.drop_index(
            op.f("ix_session_histories_last_activity_at"),
            table_name="session_histories",
            schema=schema,
        )
        op.drop_column("session_histories", "last_activity_at", schema=schema)
    if _column_exists(schema, "session_histories", "started_at"):
        op.drop_index(
            op.f("ix_session_histories_started_at"),
            table_name="session_histories",
            schema=schema,
        )
        op.drop_column("session_histories", "started_at", schema=schema)

    if _column_exists(schema, "user_quotas", "token_limit_daily"):
        op.drop_column("user_quotas", "token_limit_daily", schema=schema)
    if _column_exists(schema, "user_quotas", "plan"):
        op.drop_column("user_quotas", "plan", schema=schema)

    if _column_exists(schema, "app_users", "profile_picture"):
        op.drop_column("app_users", "profile_picture", schema=schema)

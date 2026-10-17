"""create app-domain tables for postgres app DB

Revision ID: 20260331_0004
Revises: 20260331_0003
Create Date: 2026-03-31 11:40:00
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260331_0004"
down_revision: Union[str, None] = "20260331_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _schema() -> str:
    return os.getenv("APP_DB_SCHEMA", "public").strip() or "public"


def upgrade() -> None:
    schema = _schema()
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    op.create_table(
        "app_users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("supertokens_user_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("handle", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("avatar_url", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    op.create_index(
        op.f("ix_app_users_supertokens_user_id"),
        "app_users",
        ["supertokens_user_id"],
        unique=True,
        schema=schema,
    )
    op.create_index(op.f("ix_app_users_email"), "app_users", ["email"], unique=True, schema=schema)
    op.create_index(
        op.f("ix_app_users_handle"), "app_users", ["handle"], unique=True, schema=schema
    )
    op.create_index(
        op.f("ix_app_users_created_at"),
        "app_users",
        ["created_at"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        "user_quotas",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("app_user_id", sa.String(), nullable=False),
        sa.Column("token_limit_monthly", sa.Integer(), nullable=True),
        sa.Column("credit_limit_monthly", sa.Float(), nullable=True),
        sa.Column("tokens_used_monthly", sa.Integer(), nullable=False),
        sa.Column("credits_used_monthly", sa.Float(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["app_user_id"], [f"{schema}.app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    op.create_index(
        op.f("ix_user_quotas_app_user_id"),
        "user_quotas",
        ["app_user_id"],
        unique=True,
        schema=schema,
    )

    op.create_table(
        "session_histories",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("app_user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("source_kind", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["app_user_id"], [f"{schema}.app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_histories_app_user_id"),
        "session_histories",
        ["app_user_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_histories_status"),
        "session_histories",
        ["status"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_histories_created_at"),
        "session_histories",
        ["created_at"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_histories_last_message_at"),
        "session_histories",
        ["last_message_at"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        "session_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_history_id", sa.String(), nullable=False),
        sa.Column("app_user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_history_id"],
            [f"{schema}.session_histories.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["app_user_id"], [f"{schema}.app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_messages_session_history_id"),
        "session_messages",
        ["session_history_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_messages_app_user_id"),
        "session_messages",
        ["app_user_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_session_messages_created_at"),
        "session_messages",
        ["created_at"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("app_user_id", sa.String(), nullable=False),
        sa.Column("session_history_id", sa.String(), nullable=True),
        sa.Column("llm_usage_event_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("credits_consumed", sa.Float(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["app_user_id"], [f"{schema}.app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_history_id"],
            [f"{schema}.session_histories.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=schema,
    )
    op.create_index(
        op.f("ix_usage_ledger_app_user_id"),
        "usage_ledger",
        ["app_user_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_usage_ledger_session_history_id"),
        "usage_ledger",
        ["session_history_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_usage_ledger_llm_usage_event_id"),
        "usage_ledger",
        ["llm_usage_event_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_usage_ledger_provider"),
        "usage_ledger",
        ["provider"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_usage_ledger_model"),
        "usage_ledger",
        ["model"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        op.f("ix_usage_ledger_created_at"),
        "usage_ledger",
        ["created_at"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()

    op.drop_index(op.f("ix_usage_ledger_created_at"), table_name="usage_ledger", schema=schema)
    op.drop_index(op.f("ix_usage_ledger_model"), table_name="usage_ledger", schema=schema)
    op.drop_index(op.f("ix_usage_ledger_provider"), table_name="usage_ledger", schema=schema)
    op.drop_index(
        op.f("ix_usage_ledger_llm_usage_event_id"),
        table_name="usage_ledger",
        schema=schema,
    )
    op.drop_index(
        op.f("ix_usage_ledger_session_history_id"),
        table_name="usage_ledger",
        schema=schema,
    )
    op.drop_index(op.f("ix_usage_ledger_app_user_id"), table_name="usage_ledger", schema=schema)
    op.drop_table("usage_ledger", schema=schema)

    op.drop_index(
        op.f("ix_session_messages_created_at"),
        table_name="session_messages",
        schema=schema,
    )
    op.drop_index(
        op.f("ix_session_messages_app_user_id"),
        table_name="session_messages",
        schema=schema,
    )
    op.drop_index(
        op.f("ix_session_messages_session_history_id"),
        table_name="session_messages",
        schema=schema,
    )
    op.drop_table("session_messages", schema=schema)

    op.drop_index(
        op.f("ix_session_histories_last_message_at"),
        table_name="session_histories",
        schema=schema,
    )
    op.drop_index(
        op.f("ix_session_histories_created_at"),
        table_name="session_histories",
        schema=schema,
    )
    op.drop_index(
        op.f("ix_session_histories_status"),
        table_name="session_histories",
        schema=schema,
    )
    op.drop_index(
        op.f("ix_session_histories_app_user_id"),
        table_name="session_histories",
        schema=schema,
    )
    op.drop_table("session_histories", schema=schema)

    op.drop_index(op.f("ix_user_quotas_app_user_id"), table_name="user_quotas", schema=schema)
    op.drop_table("user_quotas", schema=schema)

    op.drop_index(op.f("ix_app_users_created_at"), table_name="app_users", schema=schema)
    op.drop_index(op.f("ix_app_users_handle"), table_name="app_users", schema=schema)
    op.drop_index(op.f("ix_app_users_email"), table_name="app_users", schema=schema)
    op.drop_index(op.f("ix_app_users_supertokens_user_id"), table_name="app_users", schema=schema)
    op.drop_table("app_users", schema=schema)

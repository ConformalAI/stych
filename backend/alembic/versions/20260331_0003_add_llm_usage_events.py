"""add llm usage events table

Revision ID: 20260331_0003
Revises: 20260331_0002
Create Date: 2026-03-31 01:10:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260331_0003"
down_revision: Union[str, None] = "20260331_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("credits_consumed", sa.Float(), nullable=False),
        sa.Column("usage_metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_usage_events_user_id"),
        "llm_usage_events",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_usage_events_session_id"),
        "llm_usage_events",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_usage_events_provider"),
        "llm_usage_events",
        ["provider"],
        unique=False,
    )
    op.create_index(op.f("ix_llm_usage_events_model"), "llm_usage_events", ["model"], unique=False)
    op.create_index(
        op.f("ix_llm_usage_events_created_at"),
        "llm_usage_events",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_usage_events_created_at"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_model"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_provider"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_session_id"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_user_id"), table_name="llm_usage_events")
    op.drop_table("llm_usage_events")

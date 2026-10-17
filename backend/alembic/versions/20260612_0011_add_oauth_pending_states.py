"""add oauth_pending_states table

Revision ID: 20260612_0011
Revises: 20260604_0010
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "20260612_0011"
down_revision = "20260604_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "oauth_pending_states" in inspector.get_table_names():
        return

    op.create_table(
        "oauth_pending_states",
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column(
            "app_user_id",
            sa.String(),
            sa.ForeignKey("app_users.id"),
            nullable=False,
        ),
        sa.Column("server_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index(
        "ix_oauth_pending_states_app_user_id",
        "oauth_pending_states",
        ["app_user_id"],
    )
    op.create_index(
        "ix_oauth_pending_states_expires_at",
        "oauth_pending_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_pending_states_expires_at", table_name="oauth_pending_states")
    op.drop_index("ix_oauth_pending_states_app_user_id", table_name="oauth_pending_states")
    op.drop_table("oauth_pending_states")

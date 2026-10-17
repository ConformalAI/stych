"""add user_integrations table

Revision ID: 20260604_0010
Revises: 20260527_0009
Create Date: 2026-06-04
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260604_0010"
down_revision = "20260527_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "user_integrations" in inspector.get_table_names():
        return  # Already created by init_db() — nothing to do.

    op.create_table(
        "user_integrations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "app_user_id",
            sa.String(),
            sa.ForeignKey("app_users.id"),
            nullable=False,
        ),
        sa.Column("server_id", sa.String(64), nullable=False),
        sa.Column("credentials_enc", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_user_id", "server_id"),
    )
    op.create_index(
        "ix_user_integrations_app_user_id",
        "user_integrations",
        ["app_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_integrations_app_user_id", table_name="user_integrations")
    op.drop_table("user_integrations")

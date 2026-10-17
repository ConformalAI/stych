"""add user_connections and user_connection_tables

Revision ID: 20260816_0015
Revises: 20260724_0014
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260816_0015"
down_revision = "20260724_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = inspector.get_table_names()
    if "user_connections" not in tables:
        op.create_table(
            "user_connections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column(
                "app_user_id",
                sa.String(),
                sa.ForeignKey("app_users.id"),
                nullable=False,
            ),
            sa.Column("connector_id", sa.String(32), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("credentials_enc", sa.Text(), nullable=False),
            sa.Column("config_json", sa.Text(), nullable=True),
            sa.Column("last_tested_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_user_connections_app_user_id", "user_connections", ["app_user_id"])
        op.create_index("ix_user_connections_connector_id", "user_connections", ["connector_id"])

    if "user_connection_tables" not in inspector.get_table_names():
        op.create_table(
            "user_connection_tables",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column(
                "connection_id",
                sa.String(),
                sa.ForeignKey("user_connections.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("schema_name", sa.String(), nullable=True),
            sa.Column("table_name", sa.String(), nullable=True),
            sa.Column("custom_query", sa.Text(), nullable=True),
            sa.Column("display_name", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_user_connection_tables_connection_id",
            "user_connection_tables",
            ["connection_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_user_connection_tables_connection_id", table_name="user_connection_tables")
    op.drop_table("user_connection_tables")
    op.drop_index("ix_user_connections_connector_id", table_name="user_connections")
    op.drop_index("ix_user_connections_app_user_id", table_name="user_connections")
    op.drop_table("user_connections")

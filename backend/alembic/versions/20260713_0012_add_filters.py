"""add filters + filter_versions tables

Revision ID: 20260713_0012
Revises: 20260612_0011
Create Date: 2026-07-13

NOTE: feature/data_visualization originally cut a parallel ``*_0012`` revision
from 20260612_0011; it merged second and was re-chained onto this head as
20260719_0013_add_visualizations.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260713_0012"
down_revision = "20260612_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing = inspector.get_table_names()

    if "filters" not in existing:
        op.create_table(
            "filters",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("app_user_id", sa.String(), sa.ForeignKey("app_users.id"), nullable=False),
            sa.Column(
                "session_history_id",
                sa.String(),
                sa.ForeignKey("session_histories.id"),
                nullable=False,
            ),
            sa.Column("dataset_ref", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_filters_app_user_id", "filters", ["app_user_id"])
        op.create_index("ix_filters_session_history_id", "filters", ["session_history_id"])
        op.create_index("ix_filters_dataset_ref", "filters", ["dataset_ref"])
        op.create_index("ix_filters_created_at", "filters", ["created_at"])

    if "filter_versions" not in existing:
        op.create_table(
            "filter_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("filter_id", sa.String(), sa.ForeignKey("filters.id"), nullable=False),
            sa.Column("version_n", sa.Integer(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("author", sa.String(), nullable=False, server_default="agent"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("filter_id", "version_n"),
        )
        op.create_index("ix_filter_versions_filter_id", "filter_versions", ["filter_id"])
        op.create_index("ix_filter_versions_created_at", "filter_versions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_filter_versions_created_at", table_name="filter_versions")
    op.drop_index("ix_filter_versions_filter_id", table_name="filter_versions")
    op.drop_table("filter_versions")
    op.drop_index("ix_filters_created_at", table_name="filters")
    op.drop_index("ix_filters_dataset_ref", table_name="filters")
    op.drop_index("ix_filters_session_history_id", table_name="filters")
    op.drop_index("ix_filters_app_user_id", table_name="filters")
    op.drop_table("filters")

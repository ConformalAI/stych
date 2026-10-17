"""add visualizations + visualization_versions tables

Revision ID: 20260719_0013
Revises: 20260713_0012
Create Date: 2026-07-19

NOTE: originally cut as 20260711_0012 from 20260612_0011, in parallel with the
filters branch's 20260713_0012. Re-chained onto the filters head and renumbered
to 0013 during the data_visualization ↔ custom_filters merge, so the chain is
0011 → filters(20260713_0012) → visualizations(20260719_0013). Any DB already
stamped with the old id needs:
UPDATE alembic_version SET version_num = '20260719_0013'
WHERE version_num = '20260711_0012';
"""

import sqlalchemy as sa

from alembic import op

revision = "20260719_0013"
down_revision = "20260713_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing = inspector.get_table_names()

    if "visualizations" not in existing:
        op.create_table(
            "visualizations",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("app_user_id", sa.String(), sa.ForeignKey("app_users.id"), nullable=False),
            sa.Column(
                "session_history_id",
                sa.String(),
                sa.ForeignKey("session_histories.id"),
                nullable=False,
            ),
            sa.Column("dataset_ref", sa.String(), nullable=True),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_visualizations_app_user_id", "visualizations", ["app_user_id"])
        op.create_index(
            "ix_visualizations_session_history_id", "visualizations", ["session_history_id"]
        )
        op.create_index("ix_visualizations_created_at", "visualizations", ["created_at"])

    if "visualization_versions" not in existing:
        op.create_table(
            "visualization_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column(
                "visualization_id",
                sa.String(),
                sa.ForeignKey("visualizations.id"),
                nullable=False,
            ),
            sa.Column("version_n", sa.Integer(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("author", sa.String(), nullable=False, server_default="user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("visualization_id", "version_n"),
        )
        op.create_index(
            "ix_visualization_versions_visualization_id",
            "visualization_versions",
            ["visualization_id"],
        )
        op.create_index(
            "ix_visualization_versions_created_at", "visualization_versions", ["created_at"]
        )


def downgrade() -> None:
    op.drop_index("ix_visualization_versions_created_at", table_name="visualization_versions")
    op.drop_index("ix_visualization_versions_visualization_id", table_name="visualization_versions")
    op.drop_table("visualization_versions")
    op.drop_index("ix_visualizations_created_at", table_name="visualizations")
    op.drop_index("ix_visualizations_session_history_id", table_name="visualizations")
    op.drop_index("ix_visualizations_app_user_id", table_name="visualizations")
    op.drop_table("visualizations")

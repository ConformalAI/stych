"""add ontologies + ontology_versions tables

Revision ID: 20260827_0017
Revises: 20260818_0016
Create Date: 2026-08-27

One ontology per session (unique on session_history_id), unlike visualizations
and filters which are per-dataset: an ontology's relationships span datasets,
so it cannot be scoped to one.

Chains onto 20260818_0016 (feature/cloud_connectors' connection-metadata
revision), giving the linear chain 0014 -> 0015 -> 0016 -> 0017. It was
authored against 0014 while the ontology branch was separate; the re-chain
happened when the two branches merged, as that note instructed.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260827_0017"
down_revision = "20260818_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing = inspector.get_table_names()

    if "ontologies" not in existing:
        op.create_table(
            "ontologies",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("app_user_id", sa.String(), sa.ForeignKey("app_users.id"), nullable=False),
            sa.Column(
                "session_history_id",
                sa.String(),
                sa.ForeignKey("session_histories.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("session_history_id", name="uq_ontologies_session"),
        )
        op.create_index("ix_ontologies_app_user_id", "ontologies", ["app_user_id"])
        op.create_index("ix_ontologies_session_history_id", "ontologies", ["session_history_id"])
        op.create_index("ix_ontologies_created_at", "ontologies", ["created_at"])

    if "ontology_versions" not in existing:
        op.create_table(
            "ontology_versions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column(
                "ontology_id",
                sa.String(),
                sa.ForeignKey("ontologies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version_n", sa.Integer(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("author", sa.String(), nullable=False, server_default="user"),
            sa.Column("summary", sa.String(), nullable=True),
            sa.Column("proposed_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ontology_id", "version_n", name="uq_ontology_versions_n"),
        )
        op.create_index("ix_ontology_versions_ontology_id", "ontology_versions", ["ontology_id"])
        op.create_index("ix_ontology_versions_created_at", "ontology_versions", ["created_at"])


def downgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    existing = sa_inspect(bind).get_table_names()
    if "ontology_versions" in existing:
        op.drop_table("ontology_versions")
    if "ontologies" in existing:
        op.drop_table("ontologies")

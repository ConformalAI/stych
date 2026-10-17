"""make session-owned artifact deletion database-enforced

Revision ID: 20260724_0014
Revises: 20260719_0013
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op

revision = "20260724_0014"
down_revision = "20260719_0013"
branch_labels = None
depends_on = None


_FOREIGN_KEYS = (
    (
        "session_messages_session_history_id_fkey",
        "session_messages",
        "session_history_id",
        "session_histories",
        "id",
        "CASCADE",
    ),
    (
        "visualizations_session_history_id_fkey",
        "visualizations",
        "session_history_id",
        "session_histories",
        "id",
        "CASCADE",
    ),
    (
        "visualization_versions_visualization_id_fkey",
        "visualization_versions",
        "visualization_id",
        "visualizations",
        "id",
        "CASCADE",
    ),
    (
        "usage_ledger_session_history_id_fkey",
        "usage_ledger",
        "session_history_id",
        "session_histories",
        "id",
        "SET NULL",
    ),
    (
        "filters_session_history_id_fkey",
        "filters",
        "session_history_id",
        "session_histories",
        "id",
        "CASCADE",
    ),
    ("filter_versions_filter_id_fkey", "filter_versions", "filter_id", "filters", "id", "CASCADE"),
)


def upgrade() -> None:
    for name, source, column, target, target_column, ondelete in _FOREIGN_KEYS:
        op.drop_constraint(name, source, type_="foreignkey")
        op.create_foreign_key(name, source, target, [column], [target_column], ondelete=ondelete)


def downgrade() -> None:
    for name, source, column, target, target_column, _ondelete in _FOREIGN_KEYS:
        op.drop_constraint(name, source, type_="foreignkey")
        op.create_foreign_key(name, source, target, [column], [target_column])

"""create users table baseline

Revision ID: 20260331_0001
Revises:
Create Date: 2026-03-31 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260331_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("supertokens_id", sa.String(), nullable=True),
        sa.Column("google_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("profile_picture", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_google_id"), "users", ["google_id"], unique=True)
    op.create_index(op.f("ix_users_supertokens_id"), "users", ["supertokens_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_supertokens_id"), table_name="users")
    op.drop_index(op.f("ix_users_google_id"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")

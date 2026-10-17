"""add user handle

Revision ID: 20260331_0002
Revises: 20260331_0001
Create Date: 2026-03-31 00:30:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260331_0002"
down_revision: Union[str, None] = "20260331_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("handle", sa.String(), nullable=True))
    op.create_index(op.f("ix_users_handle"), "users", ["handle"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_handle"), table_name="users")
    op.drop_column("users", "handle")

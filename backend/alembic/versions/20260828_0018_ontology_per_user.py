"""scope ontologies to the user instead of the session

Revision ID: 20260828_0018
Revises: 20260827_0017
Create Date: 2026-08-28

An ontology is a user's agreed vocabulary, not a property of the chat session
it was typed in. Session scope forced users to redefine the same certified
metric in every new session, which defeats the point of certifying one.

Existing rows are DELETED rather than migrated. Collapsing "one per session"
into "one per user" is a many-to-one collision with no automatic right answer,
and the feature is beta with no ontologies worth preserving — a deliberate
call, not an oversight. Users start with a fresh empty ontology. Take a dump of
``ontologies`` / ``ontology_versions`` first if any content matters.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260828_0018"
down_revision = "20260827_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "ontologies" not in inspector.get_table_names():
        return

    # Versions first — they reference ontologies. The FK cascades, but being
    # explicit keeps this correct on backends where it may not.
    op.execute(sa.text("DELETE FROM ontology_versions"))
    op.execute(sa.text("DELETE FROM ontologies"))

    existing = {c["name"] for c in inspector.get_columns("ontologies")}
    uniques = {uc["name"] for uc in inspector.get_unique_constraints("ontologies")}

    # Live DBs that created the unique via PostgreSQL (not Alembic's named
    # constraint) use `ontologies_session_history_id_key` instead of
    # `uq_ontologies_session`. Drop whichever unique covers that column.
    for uc in inspector.get_unique_constraints("ontologies"):
        cols = uc.get("column_names") or []
        if "session_history_id" in cols and uc.get("name"):
            op.drop_constraint(uc["name"], "ontologies", type_="unique")
    if "session_history_id" in existing:
        op.drop_column("ontologies", "session_history_id")

    if "uq_ontologies_user" not in uniques:
        op.create_unique_constraint("uq_ontologies_user", "ontologies", ["app_user_id"])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM ontology_versions"))
    op.execute(sa.text("DELETE FROM ontologies"))
    op.drop_constraint("uq_ontologies_user", "ontologies", type_="unique")
    op.add_column(
        "ontologies",
        sa.Column(
            "session_history_id",
            sa.String(),
            sa.ForeignKey("session_histories.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_ontologies_session", "ontologies", ["session_history_id"])

"""Bring the app database schema up to date. Runs before the API starts.

What it does, depending on the state of the database:

* Fresh database (no app tables): create the schema from the current models and mark Alembic
  as being at ``head``.
* Database already tracked by Alembic: run ``alembic upgrade head``.
* Tables exist but Alembic has no history (a database built by an older ``create_all``-only
  deploy): the schema revision is unknown, so the database is left untouched and a warning is
  logged. Set ``ALEMBIC_BASELINE_REVISION`` to the revision the schema matches to stamp it and
  continue with ``upgrade head``.

Set ``RUN_MIGRATIONS=false`` to skip this step entirely.

Exit code is non-zero when a migration fails, so a broken schema stops the deploy instead of
serving traffic.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

_LOG_FORMAT = "%(asctime)s - migrate - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logger = logging.getLogger("migrate")

_DB_WAIT_ATTEMPTS = 30
_DB_WAIT_SECONDS = 2


def _enabled() -> bool:
    raw = os.getenv("RUN_MIGRATIONS", "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _wait_for_database(engine) -> bool:
    """Wait until the database accepts connections (it may still be starting)."""
    from sqlalchemy import text

    for attempt in range(1, _DB_WAIT_ATTEMPTS + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # any connection error just means "not ready yet"
            logger.info(
                "Database not ready (%d/%d): %s", attempt, _DB_WAIT_ATTEMPTS, type(exc).__name__
            )
            time.sleep(_DB_WAIT_SECONDS)
    return False


def main() -> int:
    if not _enabled():
        logger.info("RUN_MIGRATIONS is off - skipping database migrations")
        return 0

    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)

    from sqlalchemy import inspect

    from alembic import command
    from alembic.config import Config
    from database.connection import APP_DB_SCHEMA, DATABASE_URL, engine, init_db

    if DATABASE_URL.startswith("sqlite"):
        logger.info("SQLite database - skipping migrations")
        return 0

    if not _wait_for_database(engine):
        logger.error("Database is not reachable - cannot run migrations")
        return 1

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    inspector = inspect(engine)
    has_version = inspector.has_table("alembic_version", schema=APP_DB_SCHEMA)
    has_app_tables = inspector.has_table("app_users", schema=APP_DB_SCHEMA) or inspector.has_table(
        "session_histories", schema=APP_DB_SCHEMA
    )

    try:
        if has_version:
            logger.info("Alembic history found - upgrading to head")
            command.upgrade(cfg, "head")
        elif not has_app_tables:
            logger.info("Empty database - creating schema from models and stamping head")
            init_db()
            command.stamp(cfg, "head")
        else:
            baseline = os.getenv("ALEMBIC_BASELINE_REVISION", "").strip()
            if not baseline:
                logger.warning(
                    "App tables exist but Alembic has no history, so the schema revision is "
                    "unknown. Leaving the database unchanged. Set ALEMBIC_BASELINE_REVISION "
                    "to the revision this schema matches to adopt Alembic."
                )
                return 0
            logger.info("Stamping existing schema at %s, then upgrading to head", baseline)
            command.stamp(cfg, baseline)
            command.upgrade(cfg, "head")
    except Exception:
        logger.exception("Database migration failed")
        return 1

    logger.info("Database schema is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())

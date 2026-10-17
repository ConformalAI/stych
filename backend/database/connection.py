"""
Database connection and session management.
Uses SQLAlchemy with app-owned Postgres (configurable via APP_DATABASE_URL).
"""

import logging
import os

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


APP_DB_SCHEMA = os.getenv("APP_DB_SCHEMA", "public").strip() or "public"
APP_DB_REQUIRE_POSTGRES = _env_bool("APP_DB_REQUIRE_POSTGRES", True)
DATABASE_URL = os.getenv("APP_DATABASE_URL") or os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError(
        "Missing app database URL. Set APP_DATABASE_URL (preferred) or DATABASE_URL."
    )
# Normalise driver fragment *before* the require-postgres guard so that
# Heroku-style "postgres://" aliases are correctly expanded first.
#
#   postgresql://          → postgresql+psycopg://   (psycopg v3)
#   postgres://            → postgresql+psycopg://   (Heroku alias → psycopg v3)
#   postgresql+psycopg2:// → kept as-is             (explicit psycopg2 request)
#   postgresql+psycopg://  → kept as-is             (explicit psycopg3 request)
#
# A bare "postgresql://" in SQLAlchemy 2.x resolves to psycopg2 *or* psycopg3
# depending on import order, which caused "No module named 'psycopg'" when only
# psycopg2-binary was installed.  Both drivers are now declared in pyproject.toml;
# we normalise to psycopg3 for bare URLs (faster async-compatible driver).
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg" + DATABASE_URL[len("postgresql") :]
    logger.debug("Normalised DATABASE_URL driver fragment to psycopg (v3)")
elif DATABASE_URL.startswith("postgres://"):
    # Heroku-style alias: expand to canonical form first so the guard below passes.
    DATABASE_URL = "postgresql+psycopg" + DATABASE_URL[len("postgres") :]
    logger.debug("Normalised postgres:// DATABASE_URL to postgresql+psycopg (v3)")

if APP_DB_REQUIRE_POSTGRES and not DATABASE_URL.startswith("postgresql"):
    raise RuntimeError(
        "App DB must be PostgreSQL. Set APP_DATABASE_URL to a postgresql:// URL "
        "or disable with APP_DB_REQUIRE_POSTGRES=false."
    )

# SQLite-specific connection args
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=os.getenv("DEBUG", "false").lower() == "true",
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# SQLite has no schema concept; force schema off so table names don't become public.*
_metadata_schema = None if DATABASE_URL.startswith("sqlite") else APP_DB_SCHEMA
metadata = MetaData(schema=_metadata_schema)
Base = declarative_base(metadata=metadata)


def get_db():
    """
    FastAPI dependency that provides a database session.
    Automatically closes the session when the request is done.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Initialize the database by creating all tables.
    Called during application startup.
    """
    from . import models  # noqa: F401 - import to register models with Base

    # Ensure non-default schema exists before table creation.
    if not DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{APP_DB_SCHEMA}"'))

    Base.metadata.create_all(bind=engine)
    logger.info(
        "Database initialized with URL: %s (schema=%s)",
        DATABASE_URL,
        APP_DB_SCHEMA if not DATABASE_URL.startswith("sqlite") else "default",
    )

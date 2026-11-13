"""Engine creation, migrations and the ingest advisory lock."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from src.config import SOLUTION_DIR

# Arbitrary constant: all ingests take this transaction-level advisory lock so only one runs at a time.
INGEST_LOCK_KEY = 7_265_401_001


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def acquire_ingest_lock(session: Session) -> None:
    """Block until no other ingest is running; released automatically at commit/rollback."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INGEST_LOCK_KEY})


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(SOLUTION_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SOLUTION_DIR / "migrations"))
    cfg.attributes["url"] = database_url
    return cfg


def run_migrations(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)

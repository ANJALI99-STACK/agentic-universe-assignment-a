"""Fixtures: temporary Postgres databases (cloned from a migrated template) and generated datasets."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from src.config import DEFAULT_SCHEMAS_DIR, SOLUTION_DIR, load_dotenv
from src.database import make_engine, run_migrations
from src.ingest import ingest_drop
from src.reporting import generate_reports
from src.schemas import Catalogue, load_catalogue
from src.security import PiiProtector
from tests.helpers import read_reports

REPO_ROOT = SOLUTION_DIR.parent
GENERATOR = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"


def _admin_url() -> str:
    load_dotenv()
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL or DATABASE_URL (PostgreSQL) to run database tests")
    return url


def _autocommit_engine(url: str) -> Engine:
    return create_engine(url, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def catalogue() -> Catalogue:
    return load_catalogue(DEFAULT_SCHEMAS_DIR)


@pytest.fixture(scope="session")
def template_db_name() -> Iterator[tuple[str, str]]:
    """A migrated template database (also proves the Alembic migration works from scratch)."""
    admin_url = _admin_url()
    name = f"ledger_tmpl_{uuid.uuid4().hex[:10]}"
    admin = _autocommit_engine(admin_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    run_migrations(make_url(admin_url).set(database=name).render_as_string(hide_password=False))
    yield admin_url, name
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def db_url(template_db_name: tuple[str, str]) -> Iterator[str]:
    admin_url, template = template_db_name
    name = f"ledger_test_{uuid.uuid4().hex[:10]}"
    admin = _autocommit_engine(admin_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = make_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def protector() -> PiiProtector:
    return PiiProtector((Fernet.generate_key().decode(),), "h" * 48)


class Pipeline:
    """Convenience wrapper: ingest drops and read back the generated reports."""

    def __init__(self, engine: Engine, catalogue: Catalogue, protector: PiiProtector, workdir: Path) -> None:
        self.engine, self.catalogue, self.protector, self.workdir = engine, catalogue, protector, workdir
        self._reports = 0

    def ingest(self, drop: Path) -> Any:
        return ingest_drop(self.engine, self.catalogue, self.protector, drop)

    def report(self) -> dict[str, Any]:
        self._reports += 1
        out = self.workdir / f"report_{self._reports}"
        generate_reports(self.engine, self.catalogue, self.protector, out)
        return read_reports(out)

    def report_dir(self) -> Path:
        self._reports += 1
        out = self.workdir / f"report_{self._reports}"
        generate_reports(self.engine, self.catalogue, self.protector, out)
        return out

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()


@pytest.fixture
def pipeline(engine: Engine, catalogue: Catalogue, protector: PiiProtector, tmp_path: Path) -> Pipeline:
    return Pipeline(engine, catalogue, protector, tmp_path)


@pytest.fixture(scope="session")
def generated_data(tmp_path_factory: pytest.TempPathFactory) -> Callable[[int], Path]:
    """Run the *supplied, unmodified* generator once per seed (small scale)."""
    cache: dict[int, Path] = {}

    def generate(seed: int) -> Path:
        if seed not in cache:
            out = tmp_path_factory.mktemp(f"data_seed_{seed}")
            subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", "small",
                            "--out", str(out)], check=True, capture_output=True)
            cache[seed] = out
        return cache[seed]

    return generate


@pytest.fixture
def make_pipeline(template_db_name: tuple[str, str], catalogue: Catalogue, protector: PiiProtector,
                  tmp_path: Path) -> Iterator[Callable[[str], Pipeline]]:
    """Factory for extra, independent databases (e.g. a clean database to compare against)."""
    admin_url, template = template_db_name
    admin = _autocommit_engine(admin_url)
    created: list[tuple[str, Engine]] = []

    def make(label: str) -> Pipeline:
        name = f"ledger_{label}_{uuid.uuid4().hex[:8]}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
        eng = make_engine(make_url(admin_url).set(database=name).render_as_string(hide_password=False))
        created.append((name, eng))
        workdir = tmp_path / label
        workdir.mkdir()
        return Pipeline(eng, catalogue, protector, workdir)

    yield make
    for name, eng in created:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()

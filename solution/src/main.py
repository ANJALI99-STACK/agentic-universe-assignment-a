"""Command line interface.

    python -m src.main migrate
    python -m src.main ingest data/drop_1 [data/drop_2 ...]
    python -m src.main report --out reports/
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from src.config import ConfigError, Settings
from src.database import make_engine, run_migrations
from src.errors import IngestError
from src.ingest import ingest_drop
from src.logging_setup import configure_logging
from src.reporting import REPORT_FILES, generate_reports
from src.schemas import SchemaError, load_catalogue
from src.security import PiiDecryptionError, PiiProtector

logger = logging.getLogger("ledger")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main", description="Interaction ledger pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create/upgrade the database schema (alembic upgrade head)")
    ingest = sub.add_parser("ingest", help="ingest one or more drop directories, in the order given")
    ingest.add_argument("drops", nargs="+", type=Path, help="drop directory, e.g. data/drop_1")
    report = sub.add_parser("report", help="write the five report files")
    report.add_argument("--out", type=Path, required=True, help="output directory, e.g. reports/")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "migrate":
        run_migrations(settings.database_url)
        logger.info("msg=migrated")
        return 0

    catalogue = load_catalogue(settings.schemas_dir)
    protector = PiiProtector.from_settings(settings)
    engine = make_engine(settings.database_url)
    try:
        if args.command == "ingest":
            for drop_dir in args.drops:  # one transaction per drop, in the order given
                summary = ingest_drop(engine, catalogue, protector, drop_dir)
                print(f"{summary.drop_name}: ingested {summary.files_ingested} file(s), "
                      f"skipped {summary.files_skipped} already-ingested file(s)")
        else:
            generate_reports(engine, catalogue, protector, args.out)
            print(f"wrote {', '.join(REPORT_FILES)} to {args.out}")
    finally:
        engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    try:
        return run(argv)
    except (ConfigError, SchemaError, IngestError, PiiDecryptionError) as exc:
        logger.error("msg=failed error=%s", exc)
        return 1
    except SQLAlchemyError as exc:
        # Driver message only (constraint / key info); the SQL statement and bound parameters are not logged.
        logger.error("msg=database_error error_type=%s detail=%s", type(exc).__name__,
                     str(getattr(exc, "orig", "")).splitlines()[0] if getattr(exc, "orig", None) else "")
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Ingest one drop directory inside a single transaction.

Idempotency: every processed file is recorded in ``source_files`` (drop name + relative path +
SHA-256). Re-ingesting a drop skips recorded files; a recorded file whose content changed is a hard
error. Because the whole drop (rows, counters, file records) commits atomically, a crash leaves
nothing half-applied and the drop can simply be re-run.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.database import acquire_ingest_lock
from src.errors import IngestError
from src.event_ingestion import EventIngestor
from src.lead_ingestion import LeadIngestor
from src.linking import resolve_quarantine
from src.logging_setup import log_kv
from src.models import Drop, DispositionCode, SourceFile, Workflow
from src.schemas import Catalogue
from src.security import PiiProtector

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DropFile:
    kind: str  # 'leads' | 'events'
    rel_path: str
    path: Path
    workflow_id: str | None = None
    batch_id: str | None = None


@dataclass
class DropSummary:
    drop_name: str
    files_ingested: int = 0
    files_skipped: int = 0
    skipped: list[str] = field(default_factory=list)


def discover_files(drop_dir: Path, catalogue: Catalogue) -> list[DropFile]:
    """List lead files (per workflow dir, filename order) then event files (filename order)."""
    if not drop_dir.is_dir():
        raise IngestError(f"drop directory not found: {drop_dir}")
    files: list[DropFile] = []
    leads_root = drop_dir / "leads"
    if leads_root.is_dir():
        for workflow_dir in sorted(p for p in leads_root.iterdir() if p.is_dir()):
            if workflow_dir.name not in catalogue.workflows:
                raise IngestError(f"leads folder for unknown workflow '{workflow_dir.name}'")
            for csv_path in sorted(workflow_dir.glob("*.csv"), key=lambda p: p.name):
                files.append(DropFile("leads", csv_path.relative_to(drop_dir).as_posix(), csv_path,
                                      workflow_dir.name, csv_path.stem))
    events_root = drop_dir / "events"
    if events_root.is_dir():
        for jsonl in sorted(events_root.glob("*.jsonl"), key=lambda p: p.name):
            files.append(DropFile("events", jsonl.relative_to(drop_dir).as_posix(), jsonl))
    if not files:
        raise IngestError(f"no leads/*/*.csv or events/*.jsonl files found in {drop_dir}")
    return files


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_reference_data(session: Session, catalogue: Catalogue) -> None:
    """Upsert workflows and disposition codes from the schema files (FK targets)."""
    for schema in catalogue.workflows.values():
        stmt = pg_insert(Workflow).values(workflow_id=schema.workflow_id, description=schema.description)
        session.execute(stmt.on_conflict_do_update(
            index_elements=[Workflow.workflow_id], set_={"description": stmt.excluded.description}))
    for codes in catalogue.dispositions.values():
        for d in codes.values():
            stmt = pg_insert(DispositionCode).values(
                workflow_id=d.workflow_id, code=d.code, label=d.label,
                is_terminal=d.is_terminal, is_success=d.is_success)
            session.execute(stmt.on_conflict_do_update(
                index_elements=[DispositionCode.workflow_id, DispositionCode.code],
                set_={"label": stmt.excluded.label, "is_terminal": stmt.excluded.is_terminal,
                      "is_success": stmt.excluded.is_success}))


def _get_or_create_drop(session: Session, name: str) -> int:
    session.execute(pg_insert(Drop).values(name=name).on_conflict_do_nothing(index_elements=[Drop.name]))
    return session.execute(select(Drop.id).where(Drop.name == name)).scalar_one()


def ingest_drop(engine: Engine, catalogue: Catalogue, protector: PiiProtector, drop_dir: Path) -> DropSummary:
    drop_dir = drop_dir.resolve()
    summary = DropSummary(drop_name=drop_dir.name)
    with Session(engine) as session, session.begin():
        acquire_ingest_lock(session)
        sync_reference_data(session, catalogue)
        drop_id = _get_or_create_drop(session, drop_dir.name)

        recorded = {
            rel: sha for rel, sha in session.execute(
                select(SourceFile.rel_path, SourceFile.sha256).where(SourceFile.drop_id == drop_id))
        }
        pending: list[tuple[DropFile, str]] = []
        for drop_file in discover_files(drop_dir, catalogue):
            sha = file_sha256(drop_file.path)
            if drop_file.rel_path in recorded:
                if recorded[drop_file.rel_path] != sha:
                    raise IngestError(
                        f"{drop_dir.name}/{drop_file.rel_path} was ingested before with different content; "
                        "refusing to re-ingest (ledger is append-only)")
                summary.files_skipped += 1
                summary.skipped.append(drop_file.rel_path)
                continue
            pending.append((drop_file, sha))

        if pending:
            lead_ingestor = LeadIngestor(session, catalogue, protector)
            event_ingestor: EventIngestor | None = None
            for drop_file, sha in pending:  # leads first, then events (discover_files order)
                source_file_id = session.execute(insert(SourceFile).values(
                    drop_id=drop_id, kind=drop_file.kind, rel_path=drop_file.rel_path, sha256=sha,
                    workflow_id=drop_file.workflow_id, batch_id=drop_file.batch_id,
                ).returning(SourceFile.id)).scalar_one()
                if drop_file.kind == "leads":
                    stats = lead_ingestor.ingest_file(
                        source_file_id, drop_file.path, drop_file.workflow_id or "", drop_file.batch_id or "")
                else:
                    if event_ingestor is None:
                        event_ingestor = EventIngestor(session, catalogue, protector, lead_ingestor.lead_index)
                    stats = event_ingestor.ingest_file(source_file_id, drop_file.path)
                session.execute(update(SourceFile).where(SourceFile.id == source_file_id).values(**vars(stats)))
                summary.files_ingested += 1
            resolve_quarantine(session, catalogue)

    log_kv(logger, "drop_ingested", drop=summary.drop_name, files_ingested=summary.files_ingested,
           files_skipped=summary.files_skipped)
    return summary

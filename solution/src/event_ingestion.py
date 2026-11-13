"""Event ingestion.

Per line, in the order mandated by the README:
1. malformed  -> counted, skipped
2. duplicate  -> counted, skipped (``event_registry`` holds every event_id ever seen)
3. orphan     -> stored in ``quarantined_events`` (status ``orphan``)
4. unknown disposition -> stored in ``quarantined_events`` (status ``unknown_disposition``)
Otherwise the event is appended to the ledger (``interaction_events``).

Overrides never carry a phone; they are always resolved afterwards through their ``call_id``
(see ``linking.resolve_quarantine``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from src.constants import (
    EVENT_DISPOSITION_OVERRIDE,
    STATUS_ORPHAN,
    STATUS_UNKNOWN_DISPOSITION,
)
from src.event_parsing import MalformedEvent, ParsedEvent, parse_event_line
from src.logging_setup import log_kv
from src.models import EventRegistry, InteractionEvent, QuarantinedEvent
from src.schemas import Catalogue
from src.security import PiiProtector

logger = logging.getLogger(__name__)
CHUNK_SIZE = 2000


@dataclass
class EventFileStats:
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0


def iter_event_lines(path: Path) -> Iterator[tuple[int, str | None]]:
    """Yield ``(line_number, text)`` for every non-empty line; ``text`` is None if not valid UTF-8."""
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                text = raw.decode("utf-8-sig" if line_number == 1 else "utf-8")
            except UnicodeDecodeError:
                yield line_number, None
                continue
            yield line_number, text.strip()


def _chunks(items: Iterator[tuple[int, str | None]], size: int) -> Iterator[list[tuple[int, str | None]]]:
    chunk: list[tuple[int, str | None]] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def event_row(event: ParsedEvent, source_file_id: int) -> dict[str, Any]:
    """Columns shared by ledger and quarantine rows (never includes the phone)."""
    return {
        "event_id": event.event_id, "workflow_id": event.workflow_id, "call_id": event.call_id,
        "event_type": event.event_type, "occurred_at": event.occurred_at,
        "occurred_at_raw": event.occurred_at_raw, "disposition_code": event.disposition_code,
        "payload": event.payload, "source_file_id": source_file_id,
    }


class EventIngestor:
    def __init__(self, session: Session, catalogue: Catalogue, protector: PiiProtector,
                 lead_index: dict[tuple[str, str], int]) -> None:
        self._session = session
        self._catalogue = catalogue
        self._protector = protector
        self._lead_index = lead_index

    def ingest_file(self, source_file_id: int, path: Path) -> EventFileStats:
        stats = EventFileStats()
        for chunk in _chunks(iter_event_lines(path), CHUNK_SIZE):
            self._ingest_chunk(source_file_id, chunk, stats)
        log_kv(logger, "event_file_ingested", file=path.name, lines=stats.events_total,
               malformed=stats.malformed_events, duplicates=stats.duplicate_events_dropped)
        return stats

    def _ingest_chunk(self, source_file_id: int, chunk: list[tuple[int, str | None]], stats: EventFileStats) -> None:
        parsed = [(n, MalformedEvent(None) if text is None else parse_event_line(text)) for n, text in chunk]
        candidate_ids = [e.event_id for _, e in parsed if e.event_id is not None]
        seen = set(self._session.execute(
            select(EventRegistry.event_id).where(EventRegistry.event_id.in_(candidate_ids))
        ).scalars()) if candidate_ids else set()

        registry: list[dict[str, Any]] = []
        ledger: list[dict[str, Any]] = []
        quarantine: list[dict[str, Any]] = []
        for line_number, event in parsed:
            stats.events_total += 1
            if isinstance(event, MalformedEvent):  # step 1 (checked before duplicates)
                stats.malformed_events += 1
                if event.event_id is not None and event.event_id not in seen:
                    seen.add(event.event_id)
                    registry.append({"event_id": event.event_id, "source_file_id": source_file_id,
                                     "line_number": line_number})
                continue
            if event.event_id in seen:  # step 2
                stats.duplicate_events_dropped += 1
                continue
            seen.add(event.event_id)
            registry.append({"event_id": event.event_id, "source_file_id": source_file_id,
                             "line_number": line_number})
            self._classify(event, source_file_id, ledger, quarantine)

        if registry:
            self._session.execute(insert(EventRegistry), registry)
        if ledger:
            self._session.execute(insert(InteractionEvent), ledger)
        if quarantine:
            self._session.execute(insert(QuarantinedEvent), quarantine)

    def _classify(self, event: ParsedEvent, source_file_id: int, ledger: list[dict[str, Any]],
                  quarantine: list[dict[str, Any]]) -> None:
        row = event_row(event, source_file_id)
        phone_hash = self._protector.phone_hash(event.workflow_id, event.phone) if event.phone else None
        lead_id = None
        if event.event_type != EVENT_DISPOSITION_OVERRIDE and phone_hash is not None:
            lead_id = self._lead_index.get((event.workflow_id, phone_hash))
        if lead_id is None:  # step 3: orphan (overrides are always resolved later through call_id)
            quarantine.append({**row, "phone_hash": phone_hash, "lead_id": None, "status": STATUS_ORPHAN})
        elif event.is_disposition and not (
            event.disposition_code is not None
            and self._catalogue.is_known(event.workflow_id, event.disposition_code)
        ):  # step 4: unknown disposition (linked, but never reportable)
            quarantine.append({**row, "phone_hash": phone_hash, "lead_id": lead_id,
                               "status": STATUS_UNKNOWN_DISPOSITION})
        else:
            ledger.append({**row, "lead_id": lead_id})

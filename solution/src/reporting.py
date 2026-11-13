"""Report generation: reads committed database state and writes the five output files.

Reports are derived on demand from the ledger (single source of truth), so they always reflect
everything ingested so far and are identical no matter how drops were split across runs.
Sorting is done in Python (code-point order) rather than by the database collation.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from src.constants import (
    BUCKET_COMPLETED,
    BUCKET_DIALED,
    BUCKET_TELEPHONY_FAILED,
    BUCKET_YET_TO_REACH_OUT,
    STATUS_ORPHAN,
    STATUS_UNKNOWN_DISPOSITION,
)
from src.derived import Call, LedgerEvent, build_calls, lead_bucket
from src.logging_setup import log_kv
from src.models import InteractionEvent, Lead, LeadRejection, QuarantinedEvent, SourceFile
from src.normalizers import mask_phone
from src.schemas import Catalogue
from src.security import PiiProtector
from src.timestamps import format_ist, ist_date

logger = logging.getLogger(__name__)

BATCH_SUMMARY_HEADER = ["workflow_id", "batch_id", "total_leads", BUCKET_YET_TO_REACH_OUT,
                        BUCKET_TELEPHONY_FAILED, BUCKET_DIALED, BUCKET_COMPLETED]
DAILY_OVERVIEW_HEADER = ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                         "success_leads"]
LEAD_REPORT_HEADER = ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                      "connected_calls", "last_call_at", "current_disposition", "ever_success"]
REJECTIONS_HEADER = ["workflow_id", "batch_id", "row_number", "reason"]
REPORT_FILES = ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
                "data_quality.json")


@dataclass(frozen=True)
class LeadInfo:
    lead_id: int
    workflow_id: str
    external_ref: str
    batch_id: str
    pii_encrypted: str


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _atomic_write(path: Path, write_body: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        write_body(handle)
    os.replace(tmp, path)


def _write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> None:
    def body(handle: Any) -> None:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    _atomic_write(path, body)


def _load_leads(session: Session) -> list[LeadInfo]:
    rows = session.execute(select(Lead.id, Lead.workflow_id, Lead.external_ref, Lead.batch_id, Lead.pii_encrypted))
    return [LeadInfo(*row) for row in rows]


def _calls_by_lead(session: Session) -> dict[int, list[Call]]:
    """Valid ledger events grouped per lead -> calls."""
    stmt = select(
        InteractionEvent.event_id, InteractionEvent.event_type, InteractionEvent.lead_id,
        InteractionEvent.call_id, InteractionEvent.occurred_at, InteractionEvent.disposition_code,
    ).order_by(InteractionEvent.lead_id)
    calls: dict[int, list[Call]] = {}
    events = (LedgerEvent(*row) for row in session.execute(stmt).yield_per(10_000))
    for lead_id, group in groupby(events, key=lambda e: e.lead_id):
        calls[lead_id] = build_calls(group)
    return calls


def build_reports(session: Session, catalogue: Catalogue, protector: PiiProtector, out_dir: Path) -> None:
    leads = _load_leads(session)
    calls_by_lead = _calls_by_lead(session)

    batch_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    daily: dict[tuple[str, str], dict[str, Any]] = {}
    lead_rows: list[tuple[str, str, int, list[Any]]] = []

    for lead in leads:
        schema = catalogue.workflows[lead.workflow_id]
        wf = lead.workflow_id
        calls = calls_by_lead.get(lead.lead_id, [])
        bucket = lead_bucket(calls, lambda code, wf=wf: catalogue.is_terminal(wf, code))

        counts = batch_counts[(wf, lead.batch_id)]
        counts["total_leads"] += 1
        counts[bucket] += 1

        for call in calls:
            day = daily.setdefault((wf, ist_date(call.initiated_at).isoformat()),
                                   {"total": 0, "connected": 0, "success_leads": set()})
            day["total"] += 1
            day["connected"] += int(call.connected)
            if call.effective_disposition is not None and catalogue.is_success(wf, call.effective_disposition):
                day["success_leads"].add(lead.lead_id)

        with_disposition = [c for c in calls if c.effective_disposition is not None]
        current = max(with_disposition, key=lambda c: (c.initiated_at, c.call_id), default=None)
        phone = protector.decrypt_json(lead.pii_encrypted)[schema.identity_phone_field.name]
        lead_rows.append((wf, lead.external_ref, lead.lead_id, [
            wf, lead.external_ref, lead.batch_id, mask_phone(phone), bucket, len(calls),
            sum(c.connected for c in calls),
            format_ist(max(c.initiated_at for c in calls)) if calls else "",
            current.effective_disposition if current else "",
            _bool(any(c.effective_disposition is not None and catalogue.is_success(wf, c.effective_disposition)
                      for c in calls)),
        ]))

    out_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(out_dir / "batch_summary.csv", BATCH_SUMMARY_HEADER, (
        [wf, batch, c["total_leads"], c[BUCKET_YET_TO_REACH_OUT], c[BUCKET_TELEPHONY_FAILED],
         c[BUCKET_DIALED], c[BUCKET_COMPLETED]]
        for (wf, batch), c in sorted(batch_counts.items())
    ))
    _write_csv(out_dir / "daily_overview.csv", DAILY_OVERVIEW_HEADER, (
        [wf, date, d["total"], d["connected"], f"{d['connected'] / d['total']:.4f}", len(d["success_leads"])]
        for (wf, date), d in sorted(daily.items())
    ))
    _write_csv(out_dir / "lead_report.csv", LEAD_REPORT_HEADER,
               (row for _, _, _, row in sorted(lead_rows, key=lambda r: (r[0], r[1], r[2]))))

    rejections = session.execute(
        select(LeadRejection.workflow_id, LeadRejection.batch_id, LeadRejection.row_number,
               LeadRejection.reason, LeadRejection.source_file_id)
    ).all()
    _write_csv(out_dir / "rejections.csv", REJECTIONS_HEADER, (
        [r.workflow_id, r.batch_id, r.row_number, r.reason]
        for r in sorted(rejections, key=lambda r: (r.workflow_id, r.batch_id, r.row_number, r.source_file_id))
    ))

    quality = data_quality(session)
    _atomic_write(out_dir / "data_quality.json",
                  lambda handle: handle.write(json.dumps(quality, indent=2, ensure_ascii=False) + "\n"))
    log_kv(logger, "reports_written", out=out_dir, leads=len(leads), calls=sum(len(c) for c in calls_by_lead.values()))


def data_quality(session: Session) -> dict[str, Any]:
    """Counters. Per-line counters are summed from ``source_files`` (so re-ingesting a recorded file
    adds nothing); orphan / unknown counters are the *current* quarantine state."""
    def total(column: Any) -> int:
        return int(session.execute(select(func.coalesce(func.sum(column), 0))).scalar_one())

    by_reason = dict(sorted(session.execute(
        select(LeadRejection.reason, func.count()).group_by(LeadRejection.reason)).all()))

    def quarantined(status: str) -> int:
        return int(session.execute(
            select(func.count()).select_from(QuarantinedEvent).where(QuarantinedEvent.status == status)).scalar_one())

    return {
        "lead_rows_total": total(SourceFile.lead_rows_total),
        "lead_rows_rejected": sum(by_reason.values()),
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": total(SourceFile.duplicate_lead_rows_merged),
        "field_warnings": total(SourceFile.field_warnings),
        "events_total": total(SourceFile.events_total),
        "malformed_events": total(SourceFile.malformed_events),
        "duplicate_events_dropped": total(SourceFile.duplicate_events_dropped),
        "orphan_events": quarantined(STATUS_ORPHAN),
        "unknown_disposition_events": quarantined(STATUS_UNKNOWN_DISPOSITION),
    }


def generate_reports(engine: Engine, catalogue: Catalogue, protector: PiiProtector, out_dir: Path) -> None:
    """Read in one REPEATABLE READ transaction so all five files come from one consistent snapshot."""
    with Session(engine) as session:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        build_reports(session, catalogue, protector, out_dir)

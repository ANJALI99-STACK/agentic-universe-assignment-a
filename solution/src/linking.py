"""Resolution of quarantined orphan events.

Run once at the end of every drop (after leads and events are ingested). Idempotent: when nothing
new can be linked it changes nothing.

1. Phone path - an orphan with a phone hash links to the lead with the same (workflow_id, hash).
2. Call path  - a ``disposition.override`` (no phone) links to the single lead that the *other*
   linked events of the same (workflow_id, call_id) belong to. If those events point to
   several different leads the override stays an orphan (ambiguous, never guessed).

A linked event becomes a ledger row only if it passes disposition validation. A linked event with
an unknown disposition code becomes ``unknown_disposition`` and is never added to the ledger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import and_, bindparam, func, insert, select, text, update
from sqlalchemy.orm import Session

from src.constants import (
    DISPOSITION_EVENT_TYPES,
    EVENT_DISPOSITION_OVERRIDE,
    STATUS_ORPHAN,
    STATUS_PROMOTED,
    STATUS_UNKNOWN_DISPOSITION,
)
from src.logging_setup import log_kv
from src.models import InteractionEvent, Lead, QuarantinedEvent
from src.schemas import Catalogue

logger = logging.getLogger(__name__)

_Q = QuarantinedEvent.__table__

_PEER_LEADS_SQL = text("""
    WITH wanted AS (
        SELECT DISTINCT workflow_id, call_id FROM quarantined_events
        WHERE status = 'orphan' AND event_type = 'disposition.override'
    ), peers AS (
        SELECT e.workflow_id, e.call_id, e.lead_id
        FROM interaction_events e JOIN wanted w USING (workflow_id, call_id)
        UNION
        SELECT q.workflow_id, q.call_id, q.lead_id
        FROM quarantined_events q JOIN wanted w USING (workflow_id, call_id)
        WHERE q.lead_id IS NOT NULL
    )
    SELECT workflow_id, call_id, min(lead_id) AS lead_id
    FROM peers GROUP BY workflow_id, call_id HAVING count(DISTINCT lead_id) = 1
""")


@dataclass
class ResolutionStats:
    linked_by_phone: int = 0
    linked_by_call: int = 0
    promoted: int = 0
    marked_unknown: int = 0


def resolve_quarantine(session: Session, catalogue: Catalogue) -> ResolutionStats:
    stats = ResolutionStats()

    phone_matches = session.execute(
        select(*_Q.c, Lead.id.label("resolved_lead_id"))
        .join(Lead, and_(Lead.workflow_id == _Q.c.workflow_id, Lead.phone_hash == _Q.c.phone_hash))
        .where(_Q.c.status == STATUS_ORPHAN, _Q.c.event_type != EVENT_DISPOSITION_OVERRIDE)
    ).mappings().all()
    stats.linked_by_phone = len(phone_matches)
    _apply(session, catalogue, phone_matches, stats)

    call_leads = {
        (row.workflow_id, row.call_id): row.lead_id for row in session.execute(_PEER_LEADS_SQL)
    }
    if call_leads:
        overrides = session.execute(
            select(*_Q.c).where(_Q.c.status == STATUS_ORPHAN, _Q.c.event_type == EVENT_DISPOSITION_OVERRIDE)
        ).mappings().all()
        matched = [
            {**row, "resolved_lead_id": call_leads[(row["workflow_id"], row["call_id"])]}
            for row in overrides if (row["workflow_id"], row["call_id"]) in call_leads
        ]
        stats.linked_by_call = len(matched)
        _apply(session, catalogue, matched, stats)

    log_kv(logger, "quarantine_resolved", linked_by_phone=stats.linked_by_phone,
           linked_by_call=stats.linked_by_call, promoted=stats.promoted, marked_unknown=stats.marked_unknown)
    return stats


def _apply(session: Session, catalogue: Catalogue, rows: Iterable[Any], stats: ResolutionStats) -> None:
    ledger_rows: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for row in rows:
        known = row["disposition_code"] is not None and catalogue.is_known(row["workflow_id"], row["disposition_code"])
        if row["event_type"] in DISPOSITION_EVENT_TYPES and not known:
            status = STATUS_UNKNOWN_DISPOSITION
            stats.marked_unknown += 1
        else:
            status = STATUS_PROMOTED
            stats.promoted += 1
            ledger_rows.append({
                "event_id": row["event_id"], "workflow_id": row["workflow_id"],
                "lead_id": row["resolved_lead_id"], "call_id": row["call_id"], "event_type": row["event_type"],
                "occurred_at": row["occurred_at"], "occurred_at_raw": row["occurred_at_raw"],
                "disposition_code": row["disposition_code"], "payload": row["payload"],
                "source_file_id": row["source_file_id"],
            })
        updates.append({"b_event": row["event_id"], "b_status": status, "b_lead": row["resolved_lead_id"]})
    if ledger_rows:
        session.execute(insert(InteractionEvent), ledger_rows)
    if updates:
        session.execute(
            update(_Q).where(_Q.c.event_id == bindparam("b_event"))
            .values(status=bindparam("b_status"), lead_id=bindparam("b_lead"), resolved_at=func.now()),
            updates,
        )

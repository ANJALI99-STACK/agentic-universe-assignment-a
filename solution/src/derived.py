"""Pure business rules for derived facts (README "Derived facts"). No database access.

Only *valid* ledger events are passed in: linked to a lead and with a known disposition code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from src.constants import (
    BUCKET_COMPLETED,
    BUCKET_DIALED,
    BUCKET_TELEPHONY_FAILED,
    BUCKET_YET_TO_REACH_OUT,
    EVENT_CONNECTED,
    EVENT_DISPOSITION_OVERRIDE,
    EVENT_DISPOSITION_SET,
    EVENT_INITIATED,
    EVENT_TELEPHONY_FAILED,
)


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    event_type: str
    lead_id: int
    call_id: str
    occurred_at: datetime  # aware
    disposition_code: str | None


@dataclass(frozen=True)
class Call:
    lead_id: int
    call_id: str
    initiated_at: datetime
    connected: bool
    telephony_failed: bool
    effective_disposition: str | None


def _latest(events: list[LedgerEvent]) -> LedgerEvent | None:
    """Latest by occurred_at; ties broken deterministically by event_id (spec is silent on ties)."""
    return max(events, key=lambda e: (e.occurred_at, e.event_id), default=None)


def effective_disposition(events: Iterable[LedgerEvent]) -> str | None:
    """Latest ``disposition.override`` if any, else latest ``disposition.set``, else None."""
    overrides = [e for e in events if e.event_type == EVENT_DISPOSITION_OVERRIDE]
    sets = [e for e in events if e.event_type == EVENT_DISPOSITION_SET]
    chosen = _latest(overrides) or _latest(sets)
    return chosen.disposition_code if chosen else None


def build_calls(events: Iterable[LedgerEvent]) -> list[Call]:
    """Group events by (lead_id, call_id). A group is a call only if it has ``call.initiated``;
    the call date/time comes from the earliest ``call.initiated`` (normally there is exactly one)."""
    groups: dict[tuple[int, str], list[LedgerEvent]] = {}
    for event in events:
        groups.setdefault((event.lead_id, event.call_id), []).append(event)
    calls: list[Call] = []
    for (lead_id, call_id), group in groups.items():
        initiated = [e for e in group if e.event_type == EVENT_INITIATED]
        if not initiated:
            continue  # no linked call.initiated => not a call
        calls.append(Call(
            lead_id=lead_id,
            call_id=call_id,
            initiated_at=min(e.occurred_at for e in initiated),
            connected=any(e.event_type == EVENT_CONNECTED for e in group),
            telephony_failed=any(e.event_type == EVENT_TELEPHONY_FAILED for e in group),
            effective_disposition=effective_disposition(group),
        ))
    return calls


def lead_bucket(calls: list[Call], is_terminal: Callable[[str], bool]) -> str:
    """First matching bucket, in this exact priority order."""
    if not calls:
        return BUCKET_YET_TO_REACH_OUT
    if any(c.effective_disposition is not None and is_terminal(c.effective_disposition) for c in calls):
        return BUCKET_COMPLETED
    if all(c.telephony_failed for c in calls):
        return BUCKET_TELEPHONY_FAILED
    return BUCKET_DIALED

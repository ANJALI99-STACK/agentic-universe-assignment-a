"""Pure event-line parsing (no database).

A line is *malformed* (README step 1) when it is not valid JSON or has no ``event_id``.
Extension beyond the README (documented in SUBMISSION.md): a line that has an ``event_id`` but cannot
be processed at all - unknown ``event_type``, missing ``workflow_id`` / ``call_id`` or an unparseable
``occurred_at`` - is also counted as malformed, but its ``event_id`` is still registered so later
copies are recognised as duplicates.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.constants import DISPOSITION_EVENT_TYPES, EVENT_TYPES
from src.normalizers import InvalidValue, normalize_phone
from src.timestamps import InvalidTimestamp, parse_timestamp


@dataclass(frozen=True)
class MalformedEvent:
    """``event_id`` is ``None`` when the line has no usable id (nothing to register)."""

    event_id: str | None


@dataclass(frozen=True)
class ParsedEvent:
    event_id: str
    event_type: str
    workflow_id: str
    call_id: str
    occurred_at: datetime  # aware UTC
    occurred_at_raw: str
    payload: dict[str, Any]
    disposition_code: str | None
    phone: str | None = field(default=None, repr=False)  # canonical phone: in-memory only, never stored/logged

    @property
    def is_disposition(self) -> bool:
        return self.event_type in DISPOSITION_EVENT_TYPES


def _clean_str(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_event_line(line: str) -> ParsedEvent | MalformedEvent:
    try:
        data = json.loads(line)
    except ValueError:  # includes JSONDecodeError and RecursionError-free bad input
        return MalformedEvent(None)
    if not isinstance(data, dict):
        return MalformedEvent(None)
    event_id = _clean_str(data.get("event_id"))
    if event_id is None:
        return MalformedEvent(None)

    event_type = data.get("event_type")
    workflow_id = _clean_str(data.get("workflow_id"))
    call_id = _clean_str(data.get("call_id"))
    raw_ts = data.get("occurred_at")
    if event_type not in EVENT_TYPES or workflow_id is None or call_id is None or not isinstance(raw_ts, str):
        return MalformedEvent(event_id)
    try:
        occurred_at = parse_timestamp(raw_ts)
    except InvalidTimestamp:
        return MalformedEvent(event_id)

    payload = data.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    code = payload.get("disposition_code") if event_type in DISPOSITION_EVENT_TYPES else None
    disposition_code = code if isinstance(code, str) else None

    phone: str | None = None
    raw_phone = data.get("phone")
    if isinstance(raw_phone, str) and raw_phone.strip():
        try:
            phone = normalize_phone(raw_phone.strip())
        except InvalidValue:
            phone = None  # unusable phone => cannot link by phone => orphan
    return ParsedEvent(event_id, event_type, workflow_id, call_id, occurred_at, raw_ts, payload,
                       disposition_code, phone)

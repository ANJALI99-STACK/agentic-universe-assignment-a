"""Timestamp parsing and IST helpers.

Accepted input forms: UTC (``...Z``), offset-aware (``...+05:30``) and naive
(``2026-10-03 10:01:00``). Naive values are interpreted as IST (Asia/Kolkata).
All values are converted to timezone-aware UTC for storage; reports convert to IST.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE
)


class InvalidTimestamp(ValueError):
    """The text is not one of the supported timestamp forms."""


def parse_timestamp(text: str) -> datetime:
    """Return a timezone-aware UTC datetime for ``text``."""
    candidate = text.strip()
    if not _TIMESTAMP.match(candidate):
        raise InvalidTimestamp("unsupported timestamp format")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise InvalidTimestamp("invalid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)  # naive means IST
    return parsed.astimezone(timezone.utc)


def to_ist(moment: datetime) -> datetime:
    """Convert an aware datetime to IST (refuses naive input to avoid silent mistakes)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime passed to to_ist")
    return moment.astimezone(IST)


def ist_date(moment: datetime) -> date:
    return to_ist(moment).date()


def format_ist(moment: datetime) -> str:
    """ISO-8601 in IST, e.g. ``2026-10-14T17:55:24+05:30``."""
    return to_ist(moment).isoformat(timespec="seconds")

"""Pure value normalisers for lead files. No I/O, no database, no PII logging.

Every ``parse_*`` function either returns the normalised value or raises ``InvalidValue``.
Rules follow ``assignment-a-data/README.md`` ("Lead files") literally.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type hints (avoids a circular import)
    from src.schemas import FieldSpec


class InvalidValue(ValueError):
    """A present value could not be parsed for its field type."""


# --------------------------------------------------------------------------- headers
_HEADER_SEPARATORS = re.compile(r"[_\s]+")


def normalize_header(text: str) -> str:
    """Lower-case, trim, then replace every run of ``_`` / whitespace with one space."""
    return _HEADER_SEPARATORS.sub(" ", text.lower().strip())


# --------------------------------------------------------------------------- phone
_NON_DIGITS = re.compile(r"[^0-9]")


def normalize_phone(raw: str) -> str:
    """Return the canonical ``+91XXXXXXXXXX`` form or raise ``InvalidValue``.

    Strip non-digits; drop ``91`` from 12-digit numbers and ``0`` from 11-digit numbers;
    valid only if exactly 10 digits starting with 6-9.
    """
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise InvalidValue("invalid phone")
    return f"+91{digits}"


def mask_phone(canonical_phone: str) -> str:
    """``+919876543210`` -> ``+91******3210``."""
    return f"+91******{canonical_phone[-4:]}"


# --------------------------------------------------------------------------- date
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAY_FIRST_NUMERIC = re.compile(r"^(\d{2})[/-](\d{2})[/-](\d{4})$")
_DAY_MON_YEAR = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$")


def parse_date(raw: str) -> date:
    """Accept ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``DD-MM-YYYY``, ``DD-Mon-YYYY`` (day-first)."""
    text = raw.strip()
    try:
        if m := _ISO_DATE.match(text):
            year, month, day = int(m[1]), int(m[2]), int(m[3])
        elif m := _DAY_FIRST_NUMERIC.match(text):
            day, month, year = int(m[1]), int(m[2]), int(m[3])
        elif m := _DAY_MON_YEAR.match(text):
            month = _MONTHS[m[2].lower()]
            day, year = int(m[1]), int(m[3])
        else:
            raise InvalidValue("unsupported date format")
        return date(year, month, day)
    except (KeyError, ValueError) as exc:  # unknown month name or impossible date (e.g. 31/02)
        raise InvalidValue("invalid date") from exc


# --------------------------------------------------------------------------- decimal
_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_AMOUNT = re.compile(r"^\d+(?:,\d+)*(?:\.\d+)?$")
_TWO_PLACES = Decimal("0.01")


def parse_decimal(raw: str) -> Decimal:
    """Parse an amount with optional ``₹``/``Rs``/``Rs.``/``INR`` prefix and ``,`` thousands."""
    body = _CURRENCY_PREFIX.sub("", raw.strip(), count=1)
    if not _AMOUNT.match(body):  # also rejects negatives, NaN, Infinity, "N/A", "-"
        raise InvalidValue("invalid amount")
    return Decimal(body.replace(",", "")).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- enum
def parse_enum(raw: str, allowed: list[str]) -> str:
    """Case-insensitive match against ``allowed``; returns the lower-case value."""
    candidate = raw.strip().lower()
    if candidate in {value.lower() for value in allowed}:
        return candidate
    raise InvalidValue("value not in enum")


# --------------------------------------------------------------------------- dispatcher
def parse_field_value(spec: "FieldSpec", raw: str) -> str:
    """Parse ``raw`` (already trimmed, non-empty) for ``spec``; returns a storable string.

    Dates are stored as ISO ``YYYY-MM-DD`` and decimals with exactly two places.
    """
    if spec.type == "string":
        return raw
    if spec.type == "phone":
        return normalize_phone(raw)
    if spec.type == "date":
        return parse_date(raw).isoformat()
    if spec.type == "decimal":
        return format(parse_decimal(raw), "f")
    if spec.type == "enum":
        return parse_enum(raw, spec.values or [])
    raise InvalidValue(f"unsupported field type {spec.type}")  # pragma: no cover

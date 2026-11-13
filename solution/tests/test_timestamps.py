from datetime import datetime, timezone

import pytest

from src.timestamps import IST, InvalidTimestamp, format_ist, ist_date, parse_timestamp, to_ist


def test_utc_z_timestamp():
    assert parse_timestamp("2026-10-01T05:26:10Z") == datetime(2026, 10, 1, 5, 26, 10, tzinfo=timezone.utc)


def test_offset_timestamp_converted_to_utc():
    parsed = parse_timestamp("2026-10-01T10:40:19+05:30")
    assert parsed == datetime(2026, 10, 1, 5, 10, 19, tzinfo=timezone.utc)
    assert parsed.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("raw", ["2026-10-01 09:15:55", "2026-10-01T09:15:55"])
def test_naive_timestamp_is_ist(raw):
    assert parse_timestamp(raw) == datetime(2026, 10, 1, 3, 45, 55, tzinfo=timezone.utc)


def test_all_forms_can_be_compared_because_all_aware():
    values = [parse_timestamp(t) for t in ("2026-10-01T00:00:00Z", "2026-10-01 05:30:00", "2026-10-01T05:30:00+05:30")]
    assert values[0] == values[1] == values[2]


def test_ist_date_rolls_over_midnight():
    assert ist_date(parse_timestamp("2026-10-01T19:00:00Z")).isoformat() == "2026-10-02"  # 00:30 IST next day
    assert ist_date(parse_timestamp("2026-10-01T18:29:59Z")).isoformat() == "2026-10-01"


def test_format_ist():
    assert format_ist(parse_timestamp("2026-10-14T12:25:24Z")) == "2026-10-14T17:55:24+05:30"


def test_to_ist_refuses_naive():
    with pytest.raises(ValueError):
        to_ist(datetime(2026, 1, 1))


@pytest.mark.parametrize("raw", ["", "yesterday", "2026-10-01", "2026-13-01T00:00:00Z", "01/10/2026 10:00"])
def test_invalid_timestamps(raw):
    with pytest.raises(InvalidTimestamp):
        parse_timestamp(raw)


def test_ist_zone_is_asia_kolkata():
    assert str(IST) == "Asia/Kolkata"

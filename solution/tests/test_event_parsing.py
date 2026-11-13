import json

import pytest

from src.event_parsing import MalformedEvent, ParsedEvent, parse_event_line
from tests.helpers import event


def line(**kw):
    return json.dumps(event(**kw))


BASE = dict(event_id="e1", event_type="call.initiated", call_id="c1", occurred_at="2026-10-01 10:00:00",
            phone="+91 98765 43210")


@pytest.mark.parametrize("text", ["{not json", '{"event_id": "x", "event_type":', "[1, 2]", '"str"', "42", "null"])
def test_invalid_json_or_not_object_is_malformed_without_id(text):
    assert parse_event_line(text) == MalformedEvent(None)


@pytest.mark.parametrize("payload", [{}, {"event_id": None}, {"event_id": ""}, {"event_id": "  "}, {"event_id": True}])
def test_missing_or_blank_event_id_is_malformed(payload):
    data = event(**BASE)
    data.pop("event_id")
    data.update(payload)
    assert parse_event_line(json.dumps(data)) == MalformedEvent(None)


def test_valid_event_parsed_and_phone_normalised_and_time_in_utc():
    parsed = parse_event_line(line(**BASE))
    assert isinstance(parsed, ParsedEvent)
    assert parsed.phone == "+919876543210"
    assert parsed.occurred_at.isoformat() == "2026-10-01T04:30:00+00:00"  # naive = IST
    assert parsed.occurred_at_raw == "2026-10-01 10:00:00"


def test_phone_is_never_in_repr():
    assert "9876543210" not in repr(parse_event_line(line(**BASE)))


def test_event_without_phone_is_still_valid_and_has_no_phone():
    parsed = parse_event_line(line(**{**BASE, "phone": None}))
    assert isinstance(parsed, ParsedEvent) and parsed.phone is None


def test_invalid_phone_value_is_kept_as_no_phone():
    assert parse_event_line(line(**{**BASE, "phone": "garbage"})).phone is None


def test_disposition_code_extracted_for_disposition_events_only():
    p = parse_event_line(line(**{**BASE, "event_type": "disposition.set"}, disposition_code="renewed"))
    assert p.disposition_code == "renewed"
    q = parse_event_line(line(**BASE, disposition_code="renewed"))
    assert q.disposition_code is None


@pytest.mark.parametrize("change", [{"event_type": "call.exploded"}, {"workflow_id": None}, {"call_id": None},
                                    {"occurred_at": "not a time"}, {"occurred_at": None}])
def test_unprocessable_event_with_id_is_malformed_but_keeps_its_id(change):
    data = event(**BASE)
    data.update(change)
    assert parse_event_line(json.dumps(data)) == MalformedEvent("e1")

from datetime import datetime, timedelta, timezone

from src.constants import (BUCKET_COMPLETED, BUCKET_DIALED, BUCKET_TELEPHONY_FAILED, BUCKET_YET_TO_REACH_OUT)
from src.derived import Call, LedgerEvent, build_calls, effective_disposition, lead_bucket

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def ev(event_id, event_type, call_id="c1", minutes=0, code=None, lead=1):
    return LedgerEvent(event_id, event_type, lead, call_id, T0 + timedelta(minutes=minutes), code)


def test_effective_disposition_none_without_any():
    assert effective_disposition([ev("a", "call.initiated")]) is None


def test_latest_set_wins_when_no_override():
    events = [ev("a", "disposition.set", minutes=1, code="x"), ev("b", "disposition.set", minutes=5, code="y")]
    assert effective_disposition(events) == "y"


def test_override_beats_set_even_if_override_is_older():
    events = [ev("a", "disposition.set", minutes=10, code="set_code"),
              ev("b", "disposition.override", minutes=1, code="override_code")]
    assert effective_disposition(events) == "override_code"


def test_latest_override_wins_among_overrides():
    events = [ev("a", "disposition.override", minutes=1, code="o1"), ev("b", "disposition.override", minutes=9, code="o2"),
              ev("c", "disposition.set", minutes=20, code="s")]
    assert effective_disposition(events) == "o2"


def test_tie_broken_by_event_id_deterministically():
    events = [ev("b", "disposition.set", minutes=1, code="y"), ev("a", "disposition.set", minutes=1, code="x")]
    assert effective_disposition(events) == "y"
    assert effective_disposition(list(reversed(events))) == "y"


def test_arrival_order_does_not_matter():
    events = [ev("a", "disposition.set", minutes=3, code="late"), ev("b", "disposition.set", minutes=1, code="early")]
    assert effective_disposition(events) == effective_disposition(events[::-1]) == "late"


def test_call_requires_initiated_event():
    events = [ev("a", "call.connected", call_id="ghost"), ev("b", "disposition.set", call_id="ghost", code="x"),
              ev("c", "call.initiated", call_id="real")]
    calls = build_calls(events)
    assert [c.call_id for c in calls] == ["real"]


def test_call_facts():
    events = [ev("a", "call.initiated", minutes=2), ev("b", "call.connected", minutes=3),
              ev("c", "disposition.set", minutes=9, code="done"), ev("d", "call.initiated", call_id="c2", minutes=50),
              ev("e", "call.telephony_failed", call_id="c2", minutes=51)]
    first, second = sorted(build_calls(events), key=lambda c: c.call_id)
    assert first.connected and not first.telephony_failed and first.effective_disposition == "done"
    assert second.telephony_failed and not second.connected and second.effective_disposition is None
    assert first.initiated_at == T0 + timedelta(minutes=2)


def test_same_call_id_on_different_leads_is_two_calls():
    events = [ev("a", "call.initiated", lead=1), ev("b", "call.initiated", lead=2)]
    assert len(build_calls(events)) == 2


def call(disposition=None, failed=False):
    return Call(1, "c", T0, False, failed, disposition)


TERMINAL = {"renewed", "dnd"}.__contains__


def test_bucket_yet_to_reach_out_when_no_calls():
    assert lead_bucket([], TERMINAL) == BUCKET_YET_TO_REACH_OUT


def test_bucket_completed_beats_telephony_failed():
    assert lead_bucket([call("renewed", failed=True)], TERMINAL) == BUCKET_COMPLETED


def test_bucket_completed_needs_only_one_terminal_call():
    assert lead_bucket([call("callback"), call("dnd")], TERMINAL) == BUCKET_COMPLETED


def test_bucket_telephony_failed_requires_every_call_failed():
    assert lead_bucket([call(failed=True), call(failed=True)], TERMINAL) == BUCKET_TELEPHONY_FAILED
    assert lead_bucket([call(failed=True), call()], TERMINAL) == BUCKET_DIALED


def test_bucket_dialed_for_nonterminal_calls():
    assert lead_bucket([call("callback")], TERMINAL) == BUCKET_DIALED

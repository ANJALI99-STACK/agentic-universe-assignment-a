"""Event ingestion, orphan recovery and linking against a real PostgreSQL database."""
from sqlalchemy import text

from tests.helpers import MOTOR, event, motor_csv, write_drop

PHONE = "9876543210"
PHONE_FMT = "+91 98765 43210"


def leads_drop(tmp_path, name="drop_1", phone=PHONE, ref="P1", batch="batch_001.csv"):
    return write_drop(tmp_path, name, leads={MOTOR: {batch: motor_csv(f"Asha,{phone},{ref},,,")}})


def events_drop(tmp_path, name, lines, filename="events_0001.jsonl"):
    return write_drop(tmp_path, name, events={filename: lines})


def q(pipeline, sql, **params):
    with pipeline.engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def test_malformed_lines_counted_and_skipped(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    lines = [event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
             '{"event_id": "e2", "event_type": "call.init', "not json at all", '{"no_id": true}', '[1]',
             "", "   ", event("e3", "call.connected", "c1", "2026-10-01T05:01:00Z", PHONE_FMT)]
    pipeline.ingest(events_drop(tmp_path, "drop_2", lines))
    quality = pipeline.report()["data_quality.json"]
    assert quality["events_total"] == 6  # blank / whitespace-only lines are not counted
    assert quality["malformed_events"] == 4
    assert pipeline.scalar("SELECT count(*) FROM interaction_events") == 2


def test_duplicate_events_dropped_within_file_and_across_drops(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    e1 = event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT)
    pipeline.ingest(events_drop(tmp_path, "drop_2", [e1, e1]))
    pipeline.ingest(events_drop(tmp_path, "drop_3", [e1, event("e2", "call.connected", "c1", "2026-10-01T05:01:00Z", PHONE_FMT)]))
    quality = pipeline.report()["data_quality.json"]
    assert quality["duplicate_events_dropped"] == 2 and quality["events_total"] == 4
    assert pipeline.scalar("SELECT count(*) FROM interaction_events") == 2


def test_duplicate_of_an_orphan_or_unknown_event_is_still_a_duplicate(pipeline, tmp_path):
    orphan = event("orph", "call.initiated", "c9", "2026-10-01T05:00:00Z", "9000000001")
    unknown = event("unk", "disposition.set", "c1", "2026-10-01T05:00:00Z", PHONE_FMT, disposition_code="NOPE")
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [orphan, unknown, orphan, unknown]))
    quality = pipeline.report()["data_quality.json"]
    assert quality["duplicate_events_dropped"] == 2
    assert quality["orphan_events"] == 1 and quality["unknown_disposition_events"] == 1


def test_event_before_lead_is_orphan_then_recovered_when_lead_arrives(pipeline, tmp_path):
    pipeline.ingest(events_drop(tmp_path, "drop_1", [
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
        event("e2", "disposition.set", "c1", "2026-10-01T05:10:00Z", PHONE_FMT, disposition_code="renewed")]))
    quality = pipeline.report()["data_quality.json"]
    assert quality["orphan_events"] == 2
    assert pipeline.scalar("SELECT count(*) FROM interaction_events") == 0
    assert pipeline.report()["lead_report.csv"] == []

    pipeline.ingest(leads_drop(tmp_path, "drop_2"))  # lead arrives later
    quality = pipeline.report()["data_quality.json"]
    assert quality["orphan_events"] == 0  # only currently unresolved orphans are counted
    assert quality["events_total"] == 2  # per-line counters are unchanged
    assert pipeline.scalar("SELECT count(*) FROM interaction_events") == 2
    [row] = pipeline.report()["lead_report.csv"]
    assert row[4] == "completed" and row[5] == "1" and row[9] == "true"


def test_events_for_rejected_leads_stay_orphans_forever(pipeline, tmp_path):
    bad_lead = write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv(f",{PHONE},P1,,,")}})  # no name
    pipeline.ingest(bad_lead)
    pipeline.ingest(events_drop(tmp_path, "drop_2", [event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT)]))
    assert pipeline.report()["data_quality.json"]["orphan_events"] == 1


def test_event_with_invalid_phone_or_no_phone_is_orphan(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", "N/A"),
        event("e2", "call.connected", "c1", "2026-10-01T05:01:00Z")]))
    assert pipeline.report()["data_quality.json"]["orphan_events"] == 2


def test_override_links_through_call_id_to_the_calls_other_events(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("o1", "disposition.override", "c1", "2026-10-02T05:00:00Z", disposition_code="dnd"),  # before its call events
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
        event("e2", "disposition.set", "c1", "2026-10-01T05:05:00Z", PHONE_FMT, disposition_code="callback_requested")]))
    assert pipeline.report()["data_quality.json"]["orphan_events"] == 0
    [row] = pipeline.report()["lead_report.csv"]
    assert row[4] == "completed" and row[8] == "dnd"  # override beats the set; dnd is terminal


def test_override_stays_orphan_until_the_calls_events_arrive_in_a_later_drop(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("o1", "disposition.override", "c1", "2026-10-02T05:00:00Z", disposition_code="dnd")]))
    assert pipeline.report()["data_quality.json"]["orphan_events"] == 1
    assert pipeline.report()["lead_report.csv"][0][4] == "yet_to_reach_out"
    pipeline.ingest(events_drop(tmp_path, "drop_3", [event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT)]))
    quality = pipeline.report()["data_quality.json"]
    assert quality["orphan_events"] == 0
    assert pipeline.report()["lead_report.csv"][0][8] == "dnd"


def test_override_for_a_call_of_an_unknown_lead_stays_orphan(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("e1", "call.initiated", "c5", "2026-10-01T05:00:00Z", "9000000001"),  # phone of no lead
        event("o1", "disposition.override", "c5", "2026-10-02T05:00:00Z", disposition_code="dnd")]))
    assert pipeline.report()["data_quality.json"]["orphan_events"] == 2


def test_unknown_disposition_is_ignored_and_never_promoted_when_lead_arrives_later(pipeline, tmp_path):
    pipeline.ingest(events_drop(tmp_path, "drop_1", [
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
        event("e2", "disposition.set", "c1", "2026-10-01T05:05:00Z", PHONE_FMT, disposition_code="RENEWD")]))
    q1 = pipeline.report()["data_quality.json"]
    assert q1["orphan_events"] == 2 and q1["unknown_disposition_events"] == 0  # orphan check comes first
    pipeline.ingest(leads_drop(tmp_path, "drop_2"))
    q2 = pipeline.report()["data_quality.json"]
    assert q2["orphan_events"] == 0 and q2["unknown_disposition_events"] == 1
    assert pipeline.scalar("SELECT count(*) FROM interaction_events WHERE event_id = 'e2'") == 0
    assert pipeline.scalar("SELECT status FROM quarantined_events WHERE event_id = 'e2'") == "unknown_disposition"
    [row] = pipeline.report()["lead_report.csv"]
    assert row[8] == "" and row[4] == "in_progress_dialed"  # ignored for report calculations


def test_unknown_override_is_ignored_the_set_still_applies(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
        event("e2", "disposition.set", "c1", "2026-10-01T05:05:00Z", PHONE_FMT, disposition_code="callback_requested"),
        event("o1", "disposition.override", "c1", "2026-10-02T05:00:00Z", disposition_code="bogus")]))
    quality = pipeline.report()["data_quality.json"]
    assert quality["unknown_disposition_events"] == 1 and quality["orphan_events"] == 0
    assert pipeline.report()["lead_report.csv"][0][8] == "callback_requested"


def test_disposition_codes_are_checked_per_workflow(pipeline, tmp_path):
    # 'feedback_captured' is valid for policy_feedback but unknown for motor_renewal
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("e1", "disposition.set", "c1", "2026-10-01T05:05:00Z", PHONE_FMT, disposition_code="feedback_captured")]))
    assert pipeline.report()["data_quality.json"]["unknown_disposition_events"] == 1


def test_arrival_order_is_not_occurrence_order(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [  # disposition arrives before the call it belongs to
        event("e3", "disposition.set", "c1", "2026-10-01T05:20:00Z", PHONE_FMT, disposition_code="renewed"),
        event("e2", "disposition.set", "c1", "2026-10-01T05:10:00Z", PHONE_FMT, disposition_code="callback_requested"),
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT)]))
    assert pipeline.report()["lead_report.csv"][0][8] == "renewed"  # latest by occurred_at, not by arrival


def test_calls_without_initiated_event_do_not_count(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("e1", "call.connected", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
        event("e2", "disposition.set", "c1", "2026-10-01T05:05:00Z", PHONE_FMT, disposition_code="renewed")]))
    [row] = pipeline.report()["lead_report.csv"]
    assert row[4] == "yet_to_reach_out" and row[5] == "0" and row[9] == "false"
    assert pipeline.report()["daily_overview.csv"] == []


def test_event_phone_is_never_stored_in_plaintext(pipeline, tmp_path):
    pipeline.ingest(events_drop(tmp_path, "drop_1", [event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT)]))
    pipeline.ingest(leads_drop(tmp_path, "drop_2"))
    with pipeline.engine.connect() as conn:
        dump = conn.execute(text("SELECT coalesce(string_agg(t::text, ' '), '') FROM quarantined_events t")).scalar_one()
        ledger = conn.execute(text("SELECT string_agg(t::text, ' ') FROM interaction_events t")).scalar_one()
    for blob in (dump, ledger):
        assert "9876543210" not in blob and "98765 43210" not in blob


def test_timestamps_of_all_forms_land_on_the_right_ist_date(pipeline, tmp_path):
    pipeline.ingest(leads_drop(tmp_path))
    pipeline.ingest(events_drop(tmp_path, "drop_2", [
        event("a", "call.initiated", "c1", "2026-10-01T19:00:00Z", PHONE_FMT),        # 00:30 IST on 10-02
        event("b", "call.initiated", "c2", "2026-10-01T23:30:00+05:30", PHONE_FMT),   # 10-01 IST
        event("c", "call.initiated", "c3", "2026-10-01 23:59:59", PHONE_FMT)]))       # naive = IST, 10-01
    dates = {r[1]: r[2] for r in pipeline.report()["daily_overview.csv"]}
    assert dates == {"2026-10-01": "2", "2026-10-02": "1"}

"""Exact report headers, ordering, formats and calculations."""
import json

from tests.helpers import FEEDBACK, MOTOR, event, motor_csv, write_drop

P1, P2, P3 = "9876543210", "9876543211", "9876543212"


def build(pipeline, tmp_path, leads_csv, events, workflows=None):
    leads = {MOTOR: {"batch_001.csv": leads_csv}}
    leads.update(workflows or {})
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads=leads, events={"events_0001.jsonl": events}))
    return pipeline.report()


def test_exact_headers_and_file_set(pipeline, tmp_path):
    out = None
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv(f"A,{P1},P1,,,")}}))
    out = pipeline.report_dir()
    assert sorted(p.name for p in out.iterdir()) == ["batch_summary.csv", "daily_overview.csv", "data_quality.json",
                                                    "lead_report.csv", "rejections.csv"]
    first_lines = {name: (out / name).read_text(encoding="utf-8").splitlines()[0] for name in
                   ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv")}
    assert first_lines == {
        "batch_summary.csv": "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed",
        "daily_overview.csv": "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads",
        "lead_report.csv": "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success",
        "rejections.csv": "workflow_id,batch_id,row_number,reason"}
    data_quality = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
    assert list(data_quality) == ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                                  "duplicate_lead_rows_merged", "field_warnings", "events_total", "malformed_events",
                                  "duplicate_events_dropped", "orphan_events", "unknown_disposition_events"]


def test_empty_database_still_writes_five_files_with_headers(pipeline, tmp_path):
    out = pipeline.report_dir()
    assert (out / "lead_report.csv").read_text(encoding="utf-8").count("\n") == 1
    assert json.loads((out / "data_quality.json").read_text(encoding="utf-8"))["events_total"] == 0


def test_lead_report_columns_masking_and_formats(pipeline, tmp_path):
    reports = build(pipeline, tmp_path, motor_csv(f"A,{P1},POL-1,,,"), [
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", P1),
        event("e2", "call.connected", "c1", "2026-10-01T05:01:00Z", P1),
        event("e3", "disposition.set", "c1", "2026-10-01T05:10:00Z", P1, disposition_code="renewed"),
        event("e4", "call.initiated", "c2", "2026-10-02T12:25:24Z", P1)])
    assert reports["lead_report.csv"] == [[
        MOTOR, "POL-1", "batch_001", "+91******3210", "completed", "2", "1", "2026-10-02T17:55:24+05:30", "renewed", "true"]]


def test_current_disposition_is_from_the_most_recent_call_that_has_one(pipeline, tmp_path):
    reports = build(pipeline, tmp_path, motor_csv(f"A,{P1},P1,,,"), [
        event("a", "call.initiated", "c1", "2026-10-01T05:00:00Z", P1),
        event("b", "disposition.set", "c1", "2026-10-01T05:05:00Z", P1, disposition_code="renewed"),
        event("c", "call.initiated", "c2", "2026-10-03T05:00:00Z", P1),
        event("d", "disposition.set", "c2", "2026-10-03T05:05:00Z", P1, disposition_code="callback_requested"),
        event("e", "call.initiated", "c3", "2026-10-04T05:00:00Z", P1)])  # newest call has no disposition
    [row] = reports["lead_report.csv"]
    assert row[7] == "2026-10-04T10:30:00+05:30"  # last_call_at = latest initiated
    assert row[8] == "callback_requested"  # most recent call WITH a disposition
    assert row[9] == "true"  # ever_success looks at every call (c1 renewed)


def test_override_changes_effective_disposition_without_touching_original_events(pipeline, tmp_path):
    reports = build(pipeline, tmp_path, motor_csv(f"A,{P1},P1,,,"), [
        event("a", "call.initiated", "c1", "2026-10-01T05:00:00Z", P1),
        event("b", "disposition.set", "c1", "2026-10-01T05:05:00Z", P1, disposition_code="renewed"),
        event("c", "disposition.override", "c1", "2026-10-02T05:00:00Z", disposition_code="wrong_number")])
    [row] = reports["lead_report.csv"]
    assert row[8] == "wrong_number" and row[9] == "false"  # override demotes success
    assert pipeline.scalar("SELECT disposition_code FROM interaction_events WHERE event_id = 'b'") == "renewed"


def test_bucket_priority_and_batch_summary(pipeline, tmp_path):
    leads = motor_csv(*[f"L{i},987654320{i},P{i},,," for i in range(5)])
    reports = build(pipeline, tmp_path, leads, [
        # P1: dialed (connected, non-terminal), P2: telephony failed only, P3: completed even though failed,
        # P4: one failed + one plain call -> dialed, P0: no calls
        event("a1", "call.initiated", "c1", "2026-10-01T05:00:00Z", "9876543201"),
        event("a2", "disposition.set", "c1", "2026-10-01T05:05:00Z", "9876543201", disposition_code="callback_requested"),
        event("b1", "call.initiated", "c2", "2026-10-01T05:00:00Z", "9876543202"),
        event("b2", "call.telephony_failed", "c2", "2026-10-01T05:00:01Z", "9876543202"),
        event("c1", "call.initiated", "c3", "2026-10-01T05:00:00Z", "9876543203"),
        event("c2", "call.telephony_failed", "c3", "2026-10-01T05:00:01Z", "9876543203"),
        event("c3", "disposition.set", "c3", "2026-10-01T05:05:00Z", "9876543203", disposition_code="dnd"),
        event("d1", "call.initiated", "c4", "2026-10-01T05:00:00Z", "9876543204"),
        event("d2", "call.telephony_failed", "c4", "2026-10-01T05:00:01Z", "9876543204"),
        event("d3", "call.initiated", "c5", "2026-10-02T05:00:00Z", "9876543204")])
    buckets = {r[1]: r[4] for r in reports["lead_report.csv"]}
    assert buckets == {"P0": "yet_to_reach_out", "P1": "in_progress_dialed", "P2": "in_progress_telephony_failed",
                       "P3": "completed", "P4": "in_progress_dialed"}
    assert reports["batch_summary.csv"] == [[MOTOR, "batch_001", "5", "1", "1", "2", "1"]]


def test_daily_overview_counts_rate_and_distinct_success_leads(pipeline, tmp_path):
    leads = motor_csv(f"A,{P1},P1,,,", f"B,{P2},P2,,,", f"C,{P3},P3,,,")
    events = []
    n = 0

    def add(call, phone, minute, connected=False, code=None, day="2026-10-01"):
        nonlocal n
        n += 1
        events.append(event(f"i{n}", "call.initiated", call, f"{day}T05:{minute:02d}:00Z", phone))
        if connected:
            events.append(event(f"c{n}", "call.connected", call, f"{day}T05:{minute:02d}:30Z", phone))
        if code:
            events.append(event(f"d{n}", "disposition.set", call, f"{day}T06:00:00Z", phone, disposition_code=code))

    add("c1", P1, 0, True, "renewed")
    add("c2", P1, 1, True, "renewed")      # same lead succeeds twice on the same day -> 1 distinct lead
    add("c3", P2, 2, True, "callback_requested")
    add("c4", P3, 3, False)
    add("c5", P3, 4, False, day="2026-10-02")
    reports = build(pipeline, tmp_path, leads, events)
    assert reports["daily_overview.csv"] == [
        [MOTOR, "2026-10-01", "4", "3", "0.7500", "1"],
        [MOTOR, "2026-10-02", "1", "0", "0.0000", "0"]]


def test_connectivity_rate_has_exactly_four_decimals(pipeline, tmp_path):
    events = [event(f"i{i}", "call.initiated", f"c{i}", "2026-10-01T05:00:00Z", P1) for i in range(3)]
    events.append(event("k", "call.connected", "c0", "2026-10-01T05:00:30Z", P1))
    reports = build(pipeline, tmp_path, motor_csv(f"A,{P1},P1,,,"), events)
    assert reports["daily_overview.csv"][0][4] == "0.3333"


def test_sorting_is_codepoint_order_not_locale(pipeline, tmp_path):
    leads = motor_csv(f"A,{P1},b-lower,,,", f"B,{P2},B-UPPER,,,", f"C,{P3},_under,,,", "D,9876543219,10,,,")
    fb = "customer_name,phone,order_id\r\nZ,9876543210,ORD-1\r\n"
    reports = build(pipeline, tmp_path, leads, [], workflows={FEEDBACK: {"batch_001.csv": fb}})
    refs = [(r[0], r[1]) for r in reports["lead_report.csv"]]
    # workflow_id first ("motor_renewal" < "policy_feedback"), then external_ref by code point:
    # digits < uppercase < "_" < lowercase (a locale collation would interleave these differently)
    assert refs == [(MOTOR, "10"), (MOTOR, "B-UPPER"), (MOTOR, "_under"), (MOTOR, "b-lower"), (FEEDBACK, "ORD-1")]
    assert [r[0] for r in reports["batch_summary.csv"]] == [MOTOR, FEEDBACK]


def test_rejections_sorted_by_workflow_batch_and_numeric_row(pipeline, tmp_path):
    rows = [f",{P1},P{i},,," for i in range(12)]  # 12 rejected rows -> row 10 must come after row 9 (numeric)
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={
        MOTOR: {"batch_002.csv": motor_csv(*rows[:2]), "batch_001.csv": motor_csv(*rows)}}))
    reports = pipeline.report()
    got = [(r[1], int(r[2])) for r in reports["rejections.csv"]]
    assert got == sorted(got) and got[:3] == [("batch_001", 1), ("batch_001", 2), ("batch_001", 3)]
    assert (got[9], got[10]) == (("batch_001", 10), ("batch_001", 11))


def test_no_pii_in_reports(pipeline, tmp_path):
    reports_dir = None
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv(
        f"Very Secret Name,{P1},POL-1,,,", f"Another Name,bad-phone,POL-2,,,")}},
        events={"events_0001.jsonl": [event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", P1)]}))
    reports_dir = pipeline.report_dir()
    text_blob = "".join(p.read_text(encoding="utf-8") for p in reports_dir.iterdir())
    for secret in ("Very Secret Name", "Another Name", "9876543210", "+919876543210", "bad-phone"):
        assert secret not in text_blob
    assert "+91******3210" in text_blob

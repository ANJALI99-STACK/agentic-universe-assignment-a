"""Re-ingestion, atomicity, drop ordering, append-only ledger and DB constraints."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

import src.ingest as ingest_module
from src.ingest import IngestError
from tests.helpers import FEEDBACK, MOTOR, event, motor_csv, write_drop

PHONE_FMT = "+91 98765 43210"


def small_drops(tmp_path):
    d1 = write_drop(
        tmp_path, "drop_1",
        leads={MOTOR: {"batch_001.csv": motor_csv("Asha,9876543210,P1,,,", ",9876543211,P2,,,", "Bad,N/A,P3,,,")}},
        events={"events_0001.jsonl": [
            event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
            event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", PHONE_FMT),
            "{broken",
            event("e9", "call.initiated", "c9", "2026-10-01T05:00:00Z", "9123456789"),  # lead only in drop_2
            event("o1", "disposition.override", "c1", "2026-10-03T05:00:00Z", disposition_code="dnd")]})
    d2 = write_drop(
        tmp_path, "drop_2",
        leads={MOTOR: {"batch_002.csv": motor_csv("Asha again,98765 43210,P1,,,500,ta", "New,9123456789,P4,,,")}},
        events={"events_1001.jsonl": [
            event("e9", "call.initiated", "c9", "2026-10-01T05:00:00Z", "9123456789"),
            event("e2", "call.connected", "c1", "2026-10-01T05:01:00Z", PHONE_FMT)]})
    return d1, d2


def snapshot(pipeline):
    tables = ["leads", "lead_rejections", "event_registry", "interaction_events", "quarantined_events", "source_files", "drops"]
    with pipeline.engine.connect() as conn:
        counts = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}
        leads = conn.execute(text("SELECT id, pii_encrypted, attributes, updated_at FROM leads ORDER BY id")).all()
        quarantine = conn.execute(text("SELECT event_id, status, lead_id, resolved_at FROM quarantined_events ORDER BY 1")).all()
    return counts, leads, quarantine


def test_reingesting_the_same_drops_changes_nothing(pipeline, tmp_path):
    d1, d2 = small_drops(tmp_path)
    pipeline.ingest(d1)
    pipeline.ingest(d2)
    before, reports_before = snapshot(pipeline), pipeline.report()
    for drop in (d1, d2, d1):
        summary = pipeline.ingest(drop)
        assert summary.files_ingested == 0 and summary.files_skipped > 0
    assert snapshot(pipeline) == before  # rows, encrypted blobs, timestamps - byte for byte
    assert pipeline.report() == reports_before


def test_sequential_equals_clean_database_equals_single_pass(pipeline, tmp_path, catalogue, protector, db_url, template_db_name):
    from sqlalchemy import create_engine
    from tests.conftest import Pipeline
    from tests.reference_model import run_reference
    from src.config import DEFAULT_SCHEMAS_DIR

    d1, d2 = small_drops(tmp_path)
    pipeline.ingest(d1)
    mid = pipeline.report()
    pipeline.ingest(d2)
    final = pipeline.report()
    assert mid["data_quality.json"]["orphan_events"] != final["data_quality.json"]["orphan_events"]  # drop_2 resolved some

    admin_url, template = template_db_name
    from sqlalchemy.engine import make_url
    fresh_name = "ledger_fresh_check"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{fresh_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{fresh_name}" TEMPLATE "{template}"'))
    fresh_engine = create_engine(make_url(admin_url).set(database=fresh_name).render_as_string(hide_password=False))
    try:
        fresh = Pipeline(fresh_engine, catalogue, protector, tmp_path / "fresh")
        fresh.workdir.mkdir()
        fresh.ingest(d1)
        fresh.ingest(d2)
        assert fresh.report() == final
    finally:
        fresh_engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{fresh_name}" WITH (FORCE)'))
        admin.dispose()

    expected = run_reference([d1, d2], DEFAULT_SCHEMAS_DIR)
    assert final["data_quality.json"] == expected["data_quality.json"]
    for name in ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv"):
        assert final[name] == expected[name], name


def test_changed_content_of_an_ingested_file_is_refused(pipeline, tmp_path):
    d1, _ = small_drops(tmp_path)
    pipeline.ingest(d1)
    before = snapshot(pipeline)
    (d1 / "leads" / MOTOR / "batch_001.csv").write_text(motor_csv("Other,9876543299,P7,,,"), encoding="utf-8")
    with pytest.raises(IngestError, match="different content"):
        pipeline.ingest(d1)
    assert snapshot(pipeline) == before


def test_new_file_added_to_an_already_ingested_drop_is_picked_up_once(pipeline, tmp_path):
    d1, _ = small_drops(tmp_path)
    pipeline.ingest(d1)
    (d1 / "events" / "events_0002.jsonl").write_text(
        '{"event_id": "late", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", '
        '"occurred_at": "2026-10-01T05:02:00Z", "phone": "9876543210", "payload": {}}\n')
    assert pipeline.ingest(d1).files_ingested == 1
    assert pipeline.ingest(d1).files_ingested == 0
    assert pipeline.scalar("SELECT count(*) FROM interaction_events WHERE event_id = 'late'") == 1


def test_failure_halfway_rolls_back_everything_and_a_retry_gives_the_same_result(pipeline, tmp_path, monkeypatch):
    d1, d2 = small_drops(tmp_path)
    pipeline.ingest(d1)
    before = snapshot(pipeline)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after files were processed")

    monkeypatch.setattr(ingest_module, "resolve_quarantine", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        pipeline.ingest(d2)
    assert snapshot(pipeline) == before  # leads, events, counters and file records all rolled back
    assert pipeline.scalar("SELECT count(*) FROM drops WHERE name = 'drop_2'") == 0
    monkeypatch.undo()

    pipeline.ingest(d2)
    from src.config import DEFAULT_SCHEMAS_DIR
    from tests.reference_model import run_reference

    recovered = pipeline.report()
    expected = run_reference([d1, d2], DEFAULT_SCHEMAS_DIR)
    assert recovered["data_quality.json"] == expected["data_quality.json"]  # nothing double counted or lost
    assert recovered["data_quality.json"]["lead_rows_total"] == 5
    assert recovered["lead_report.csv"] == expected["lead_report.csv"]


def test_unknown_workflow_folder_aborts_before_changing_anything(pipeline, tmp_path):
    d1, _ = small_drops(tmp_path)
    (d1 / "leads" / "mystery_workflow").mkdir()
    with pytest.raises(IngestError, match="unknown workflow"):
        pipeline.ingest(d1)
    assert snapshot(pipeline)[0]["leads"] == 0


def test_empty_or_missing_drop_is_an_error(pipeline, tmp_path):
    with pytest.raises(IngestError):
        pipeline.ingest(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(IngestError):
        pipeline.ingest(tmp_path / "empty")


def test_ledger_is_append_only_at_the_database_level(pipeline, tmp_path):
    d1, d2 = small_drops(tmp_path)
    pipeline.ingest(d1)
    pipeline.ingest(d2)
    for statement in ("UPDATE interaction_events SET call_id = 'x'", "DELETE FROM interaction_events",
                      "TRUNCATE interaction_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            with pipeline.engine.begin() as conn:
                conn.execute(text(statement))
    assert pipeline.scalar("SELECT count(*) FROM interaction_events") > 0


def test_ledger_rows_are_never_rewritten_by_later_drops(pipeline, tmp_path):
    d1, d2 = small_drops(tmp_path)
    pipeline.ingest(d1)
    with pipeline.engine.connect() as conn:
        first = conn.execute(text("SELECT * FROM interaction_events ORDER BY id")).all()
    pipeline.ingest(d2)
    with pipeline.engine.connect() as conn:
        after = conn.execute(text("SELECT * FROM interaction_events ORDER BY id")).all()
    assert after[:len(first)] == first and len(after) > len(first)


def test_database_constraints_block_duplicates(pipeline, tmp_path):
    d1, _ = small_drops(tmp_path)
    pipeline.ingest(d1)
    with pytest.raises(IntegrityError):  # duplicate event id (global uniqueness)
        with pipeline.engine.begin() as conn:
            conn.execute(text("INSERT INTO event_registry (event_id, source_file_id, line_number) "
                              "SELECT event_id, source_file_id, 99 FROM event_registry LIMIT 1"))
    with pytest.raises(IntegrityError):  # duplicate lead identity
        with pipeline.engine.begin() as conn:
            conn.execute(text("INSERT INTO leads (workflow_id, phone_hash, external_ref, batch_id, source_file_id, "
                              "pii_encrypted, attributes) SELECT workflow_id, phone_hash, external_ref, batch_id, "
                              "source_file_id, pii_encrypted, attributes FROM leads LIMIT 1"))
    with pytest.raises(IntegrityError):  # ledger event must be registered first
        with pipeline.engine.begin() as conn:
            conn.execute(text("INSERT INTO interaction_events (event_id, workflow_id, lead_id, call_id, event_type, "
                              "occurred_at, occurred_at_raw, payload, source_file_id) "
                              "SELECT 'unregistered', workflow_id, id, 'c', 'call.initiated', now(), 'x', '{}', source_file_id FROM leads LIMIT 1"))


def test_quarantine_never_marks_a_row_linked_without_a_lead(pipeline, tmp_path):
    d1, _ = small_drops(tmp_path)
    pipeline.ingest(d1)
    with pytest.raises(IntegrityError):
        with pipeline.engine.begin() as conn:
            conn.execute(text("UPDATE quarantined_events SET status = 'promoted' WHERE lead_id IS NULL"))


def test_ingest_order_is_invocation_order_first_valid_row_owns_batch_id(pipeline, tmp_path):
    a = write_drop(tmp_path, "drop_a", leads={MOTOR: {"batch_009.csv": motor_csv("X,9876543210,P-a,,,")}})
    b = write_drop(tmp_path, "drop_b", leads={MOTOR: {"batch_001.csv": motor_csv("Y,9876543210,P-b,,,")}})
    pipeline.ingest(a)
    pipeline.ingest(b)
    assert pipeline.scalar("SELECT batch_id FROM leads") == "batch_009"  # first ingested wins, not lowest name


def test_workflow_and_disposition_reference_data_synced_from_schema_files(pipeline, tmp_path, catalogue):
    d1, _ = small_drops(tmp_path)
    pipeline.ingest(d1)
    assert pipeline.scalar("SELECT count(*) FROM workflows") == len(catalogue.workflows)
    expected = sum(len(v) for v in catalogue.dispositions.values())
    assert pipeline.scalar("SELECT count(*) FROM dispositions") == expected
    assert FEEDBACK in catalogue.workflows


def test_two_concurrent_ingests_of_the_same_drop_are_serialised(db_url, catalogue, protector, tmp_path):
    """The advisory lock makes the second run wait, then skip the already-recorded files."""
    from concurrent.futures import ThreadPoolExecutor

    from src.database import make_engine
    from src.ingest import ingest_drop
    from tests.conftest import Pipeline

    d1, _ = small_drops(tmp_path)
    engines = [make_engine(db_url) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ingest_drop, e, catalogue, protector, d1) for e in engines]
            summaries = [f.result(timeout=60) for f in futures]
        assert sorted(s.files_ingested for s in summaries) == [0, 2]  # exactly one run did the work (drop_1 has 2 files)
        check = Pipeline(engines[0], catalogue, protector, tmp_path / "check")
        check.workdir.mkdir()
        assert check.report()["data_quality.json"]["lead_rows_total"] == 3  # counted once, not twice
        assert check.scalar("SELECT count(*) FROM leads") == 1  # 1 valid row, 2 rejected
    finally:
        for e in engines:
            e.dispose()


def test_unreadable_lead_file_aborts_the_drop_cleanly(pipeline, tmp_path):
    d1, _ = small_drops(tmp_path)
    (d1 / "leads" / MOTOR / "batch_001.csv").write_bytes(b"customer_name,phone,policy_number\n\xff\xfe\x80,98765,P\n")
    with pytest.raises(IngestError, match="cannot read lead file batch_001.csv: UnicodeDecodeError") as info:
        pipeline.ingest(d1)
    assert "98765" not in str(info.value)
    assert snapshot(pipeline)[0]["leads"] == 0 and snapshot(pipeline)[0]["drops"] == 0

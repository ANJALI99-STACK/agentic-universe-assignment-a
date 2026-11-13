"""Lead ingestion against a real PostgreSQL database."""
from sqlalchemy import text

from tests.helpers import FEEDBACK, MOTOR, motor_csv, write_drop


def lead_row(name, phone, ref, expiry="", premium="", lang=""):
    return f"{name},{phone},{ref},{expiry},{premium},{lang}"


def fetch_leads(pipeline):
    with pipeline.engine.connect() as conn:
        return conn.execute(text(
            "SELECT workflow_id, external_ref, batch_id, pii_encrypted, attributes FROM leads ORDER BY id")).all()


def test_lead_created_with_permanent_batch_id_and_encrypted_pii(pipeline, tmp_path):
    drop = write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv(
        lead_row("Asha Rao", "98765 43210", "POL-1", "31-Oct-2026", '"₹27,554"', "HI"))}})
    pipeline.ingest(drop)
    [lead] = fetch_leads(pipeline)
    assert (lead.workflow_id, lead.external_ref, lead.batch_id) == (MOTOR, "POL-1", "batch_001")
    assert "Asha" not in lead.pii_encrypted and "9876543210" not in lead.pii_encrypted
    assert pipeline.protector.decrypt_json(lead.pii_encrypted) == {"customer_name": "Asha Rao", "phone": "+919876543210"}
    assert lead.attributes == {"policy_expiry": "2026-10-31", "premium_amount": "27554.00", "preferred_language": "hi"}
    assert pipeline.scalar("SELECT char_length(phone_hash) FROM leads") == 64


def test_duplicate_rows_merge_into_one_lead_and_keep_original_batch(pipeline, tmp_path):
    first = motor_csv(lead_row("Asha Rao", "9876543210", "POL-1", "01/12/2026", "1000", "hi"))
    # later batch: phone in another format, new name, blank premium, invalid language, new expiry
    second = motor_csv(lead_row("Asha R", "+91 98765-43210", "POL-1", "02-12-2026", "", "Hindi"))
    d1 = write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": first}})
    d2 = write_drop(tmp_path, "drop_2", leads={MOTOR: {"batch_002.csv": second}})
    pipeline.ingest(d1)
    pipeline.ingest(d2)
    [lead] = fetch_leads(pipeline)
    assert lead.batch_id == "batch_001"
    assert pipeline.protector.decrypt_json(lead.pii_encrypted)["customer_name"] == "Asha R"  # non-null overwrites
    assert lead.attributes["premium_amount"] == "1000.00"  # null incoming keeps previous value
    assert lead.attributes["preferred_language"] == "hi"  # invalid incoming -> null -> keeps previous
    assert lead.attributes["policy_expiry"] == "2026-12-02"
    quality = pipeline.report()["data_quality.json"]
    assert quality["duplicate_lead_rows_merged"] == 1
    assert quality["field_warnings"] == 1  # the invalid language on the (kept) duplicate row
    assert quality["lead_rows_total"] == 2


def test_duplicate_within_same_file_and_each_duplicate_counted_once(pipeline, tmp_path):
    text_ = motor_csv(lead_row("A", "9876543210", "P1"), lead_row("A2", "09876543210", "P1"),
                      lead_row("A3", "+919876543210", "P1"))
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": text_}}))
    assert len(fetch_leads(pipeline)) == 1
    assert pipeline.report()["data_quality.json"]["duplicate_lead_rows_merged"] == 2


def test_defaults_apply_only_after_merging(pipeline, tmp_path):
    d1 = write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv(
        lead_row("A", "9876543210", "P1"), lead_row("B", "9876543211", "P2", lang="bogus"))}})
    d2 = write_drop(tmp_path, "drop_2", leads={MOTOR: {"batch_002.csv": motor_csv(lead_row("A", "9876543210", "P1", lang="ta"))}})
    pipeline.ingest(d1)
    pipeline.ingest(d2)
    langs = {l.external_ref: l.attributes["preferred_language"] for l in fetch_leads(pipeline)}
    assert langs == {"P1": "ta", "P2": "en"}  # merged value wins over default; default fills the rest


def test_rejected_rows_do_not_become_leads_and_store_no_pii(pipeline, tmp_path):
    csv_text = motor_csv(
        lead_row("", "9876543210", "P1"),            # row 1 missing name
        lead_row("Secret Name", "N/A", "P2"),         # row 2 invalid phone
        lead_row("Other Name", "", "P3"),             # row 3 missing phone
        lead_row("Ok Name", "9876543212", ""),        # row 4 missing policy
        lead_row("Good Name", "9876543213", "P5"),    # row 5 valid
        lead_row("Bad Phone Name", "5876543210", ""),  # row 6 invalid phone (phone before policy)
    )
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": csv_text}}))
    assert [l.external_ref for l in fetch_leads(pipeline)] == ["P5"]
    with pipeline.engine.connect() as conn:
        rows = conn.execute(text("SELECT row_number, reason, batch_id FROM lead_rejections ORDER BY row_number")).all()
        dump = conn.execute(text("SELECT string_agg(t::text, ' ') FROM lead_rejections t")).scalar_one()
    assert [(r.row_number, r.reason) for r in rows] == [
        (1, "missing_required:customer_name"), (2, "invalid_phone"), (3, "missing_required:phone"),
        (4, "missing_required:policy_number"), (6, "invalid_phone")]
    assert all(r.batch_id == "batch_001" for r in rows)
    for secret in ("Secret Name", "Other Name", "9876543210", "N/A", "Ok Name"):
        assert secret not in dump


def test_warnings_count_only_for_rows_that_are_not_rejected(pipeline, tmp_path):
    csv_text = motor_csv(
        lead_row("A", "9876543210", "P1", expiry="bad-date"),   # kept, 1 warning
        lead_row("B", "bad", "P2", expiry="bad-date", premium="-1"),  # rejected: its warnings must not count
    )
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": csv_text}}))
    quality = pipeline.report()["data_quality.json"]
    assert quality["field_warnings"] == 1 and quality["lead_rows_rejected"] == 1


def test_blank_rows_not_counted_and_row_numbers_skip_blanks(pipeline, tmp_path):
    csv_text = motor_csv(",,,,,", lead_row("A", "9876543210", "P1"), ",,,,,", lead_row("", "9876543211", "P2"))
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": csv_text}}))
    quality = pipeline.report()["data_quality.json"]
    assert quality["lead_rows_total"] == 2
    assert pipeline.scalar("SELECT row_number FROM lead_rejections") == 2  # second non-blank row


def test_renamed_reordered_columns_in_later_batch(pipeline, tmp_path):
    later = ("Language,Insured Name,Policy No,Mobile No,agent_notes\r\n"
             "hi,Meera,POL-9,98765 11111,hello\r\n")
    pipeline.ingest(write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_003.csv": later}}))
    [lead] = fetch_leads(pipeline)
    assert lead.external_ref == "POL-9" and lead.attributes["preferred_language"] == "hi"


def test_same_phone_in_two_workflows_gives_two_leads(pipeline, tmp_path):
    fb = "customer_name,phone,order_id\r\nAsha,9876543210,ORD-1\r\n"
    drop = write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv(lead_row("Asha", "9876543210", "P1"))},
                                                 FEEDBACK: {"batch_001.csv": fb}})
    pipeline.ingest(drop)
    assert len(fetch_leads(pipeline)) == 2
    assert pipeline.scalar("SELECT count(DISTINCT phone_hash) FROM leads") == 2
    assert pipeline.report()["data_quality.json"]["duplicate_lead_rows_merged"] == 0


def test_files_processed_in_filename_order_so_earliest_file_owns_the_lead(pipeline, tmp_path):
    drop = write_drop(tmp_path, "drop_1", leads={MOTOR: {
        "batch_002.csv": motor_csv(lead_row("Later", "9876543210", "P-late")),
        "batch_001.csv": motor_csv(lead_row("Earlier", "9876543210", "P-early"))}})
    pipeline.ingest(drop)
    [lead] = fetch_leads(pipeline)
    assert lead.batch_id == "batch_001"
    assert lead.external_ref == "P-late"  # later non-null value overwrites

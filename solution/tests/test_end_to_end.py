"""End-to-end checks on data from the supplied generator, for several seeds.

For every seed:
  1. ingest drop_1 -> intermediate reports must equal the reference model run on drop_1 alone
  2. ingest drop_2 -> final reports must equal the reference model run on both drops
  3. re-ingest both drops -> database state and reports are unchanged
  4. a clean database that ingests both drops gives identical reports
  5. no plaintext PII anywhere in the database or the reports
The reference model is an independent single-pass implementation (tests/reference_model.py).
"""
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from src.config import DEFAULT_SCHEMAS_DIR
from src.lead_parsing import iter_raw_rows
from src.normalizers import InvalidValue, normalize_phone
from tests.reference_model import run_reference

SEEDS = [42, 7, 2026, 12345]
TABLES = ["workflows", "dispositions", "drops", "source_files", "leads", "lead_rejections", "event_registry",
          "interaction_events", "quarantined_events"]
CSV_FILES = ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv")


def assert_matches_reference(reports, expected, label):
    assert reports["data_quality.json"] == expected["data_quality.json"], f"{label}: data_quality.json"
    for name in CSV_FILES:
        assert reports[name] == expected[name], f"{label}: {name}"


def db_state(pipeline):
    with pipeline.engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT coalesce(string_agg(x::text, E'\\n' ORDER BY x::text), '') FROM {t} x")).scalar_one()
                for t in TABLES}


def plaintext_pii(data_dir: Path, catalogue):
    """Names and canonical phone digits found in the raw input files."""
    names, phones = set(), set()
    for csv_path in data_dir.glob("drop_*/leads/*/*.csv"):
        schema = catalogue.workflows[csv_path.parent.name]
        for _, raw in iter_raw_rows(csv_path, schema):
            if raw.get("customer_name"):
                names.add(raw["customer_name"])
            try:
                phones.add(normalize_phone(raw["phone"])[3:]) if raw.get("phone") else None
            except InvalidValue:
                pass
    for jsonl in data_dir.glob("drop_*/events/*.jsonl"):
        for match in re.finditer(r'"phone": "([^"]+)"', jsonl.read_text(encoding="utf-8")):
            try:
                phones.add(normalize_phone(match.group(1))[3:])
            except InvalidValue:
                pass
    return names, phones


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_data_sequential_reingest_clean_db_and_reference(seed, generated_data, pipeline, make_pipeline, catalogue):
    data = generated_data(seed)
    drop_1, drop_2 = data / "drop_1", data / "drop_2"

    pipeline.ingest(drop_1)
    intermediate = pipeline.report()
    assert_matches_reference(intermediate, run_reference([drop_1], DEFAULT_SCHEMAS_DIR), f"seed {seed} after drop_1")

    pipeline.ingest(drop_2)
    final = pipeline.report()
    expected = run_reference([drop_1, drop_2], DEFAULT_SCHEMAS_DIR)
    assert_matches_reference(final, expected, f"seed {seed} final")

    # the data really exercises the planted data-quality problems
    quality = final["data_quality.json"]
    assert quality["lead_rows_rejected"] > 0 and quality["duplicate_lead_rows_merged"] > 0
    assert quality["field_warnings"] > 0 and quality["malformed_events"] > 0 and quality["duplicate_events_dropped"] > 0
    assert quality["orphan_events"] > 0 and quality["unknown_disposition_events"] > 0
    assert intermediate["data_quality.json"]["orphan_events"] > quality["orphan_events"]  # drop_2 recovered orphans
    assert any(r[4] == "completed" for r in final["lead_report.csv"])
    assert any(r[4] == "in_progress_telephony_failed" for r in final["lead_report.csv"])

    # re-ingestion changes nothing
    state_before = db_state(pipeline)
    for drop in (drop_1, drop_2):
        assert pipeline.ingest(drop).files_ingested == 0
    assert db_state(pipeline) == state_before
    assert pipeline.report() == final

    # clean database, both drops in order => identical
    clean = make_pipeline("clean")
    clean.ingest(drop_1)
    clean.ingest(drop_2)
    assert clean.report() == final

    # data_quality internal consistency
    with pipeline.engine.connect() as conn:
        assert conn.execute(text("SELECT sum(lead_rows_rejected) FROM source_files")).scalar_one() == quality["lead_rows_rejected"]
        assert conn.execute(text("SELECT count(*) FROM lead_rejections")).scalar_one() == quality["lead_rows_rejected"]
        registered = conn.execute(text("SELECT count(*) FROM event_registry")).scalar_one()
        ledger = conn.execute(text("SELECT count(*) FROM interaction_events")).scalar_one()
    # every non-malformed, non-duplicate line is registered once and ends up in exactly one place
    assert registered == quality["events_total"] - quality["malformed_events"] - quality["duplicate_events_dropped"]
    assert ledger + quality["orphan_events"] + quality["unknown_disposition_events"] == registered


@pytest.mark.parametrize("seed", [42, 2026])
def test_no_plaintext_pii_in_database_or_reports(seed, generated_data, pipeline, catalogue):
    data = generated_data(seed)
    pipeline.ingest(data / "drop_1")
    pipeline.ingest(data / "drop_2")
    out = pipeline.report_dir()
    blobs = list(db_state(pipeline).values()) + [p.read_text(encoding="utf-8") for p in out.iterdir()]
    haystack = "\n".join(blobs)
    names, phones = plaintext_pii(data, catalogue)
    assert len(names) > 50 and len(phones) > 500  # the scan itself is meaningful
    for name in names:
        assert name not in haystack, "customer name found in plaintext"
    found_digit_runs = set(re.findall(r"(?=(\d{10}))", haystack))
    leaked = phones & found_digit_runs
    assert not leaked, f"{len(leaked)} phone numbers appear in plaintext"


@pytest.mark.parametrize("seed", [42])
def test_lead_phones_are_masked_to_last_four_digits(seed, generated_data, pipeline):
    data = generated_data(seed)
    pipeline.ingest(data / "drop_1")
    pipeline.ingest(data / "drop_2")
    rows = pipeline.report()["lead_report.csv"]
    assert rows and all(re.fullmatch(r"\+91\*{6}\d{4}", r[3]) for r in rows)

"""The documented commands, run exactly as a user would run them."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.config import SOLUTION_DIR
from tests.helpers import MOTOR, event, motor_csv, read_reports, write_drop


@pytest.fixture
def cli_env(db_url, tmp_path):
    """Hermetic environment for the CLI subprocess.

    ``LEDGER_ENV_FILE`` points at a file that does not exist, so a developer's real ``solution/.env`` is never
    loaded and the test only sees the variables set here.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PII_", "DATABASE_URL", "TEST_DATABASE_URL", "LEDGER_"))}
    env.update(DATABASE_URL=db_url, PII_ENCRYPTION_KEY=Fernet.generate_key().decode(), PII_HMAC_KEY="s" * 48,
               LEDGER_ENV_FILE=str(tmp_path / "no-such.env"))
    return env


def run_cli(env, *args):
    return subprocess.run([sys.executable, "-m", "src.main", *args], cwd=SOLUTION_DIR, env=env,
                          capture_output=True, text=True)


def test_ingest_two_drops_then_report(cli_env, tmp_path):
    d1 = write_drop(tmp_path, "drop_1", leads={MOTOR: {"batch_001.csv": motor_csv("Asha Rao,9876543210,P1,,,")}})
    d2 = write_drop(tmp_path, "drop_2", events={"events_1001.jsonl": [
        event("e1", "call.initiated", "c1", "2026-10-01T05:00:00Z", "9876543210")]})
    out = tmp_path / "reports"
    first = run_cli(cli_env, "ingest", str(d1))
    assert first.returncode == 0 and "ingested 1 file" in first.stdout
    second = run_cli(cli_env, "ingest", str(d2))
    assert second.returncode == 0
    again = run_cli(cli_env, "ingest", str(d1), str(d2))  # several drops, already ingested
    assert again.returncode == 0 and again.stdout.count("ingested 0 file") == 2
    report = run_cli(cli_env, "report", "--out", str(out))
    assert report.returncode == 0
    assert read_reports(out)["lead_report.csv"][0][4] == "in_progress_dialed"
    for stream in (first, second, again, report):  # no PII in any output or log line
        assert "Asha" not in stream.stdout + stream.stderr and "9876543210" not in stream.stdout + stream.stderr


def test_migrate_is_repeatable(cli_env):
    assert run_cli(cli_env, "migrate").returncode == 0
    assert run_cli(cli_env, "migrate").returncode == 0


def test_missing_secrets_fail_cleanly_without_traceback(cli_env, tmp_path):
    env = {k: v for k, v in cli_env.items() if k != "PII_ENCRYPTION_KEY"}
    result = run_cli(env, "report", "--out", str(tmp_path / "r"))
    assert result.returncode == 1 and "PII_ENCRYPTION_KEY" in result.stderr and "Traceback" not in result.stderr


def test_short_hmac_key_rejected(cli_env, tmp_path):
    result = run_cli({**cli_env, "PII_HMAC_KEY": "short"}, "report", "--out", str(tmp_path / "r"))
    assert result.returncode == 1 and "PII_HMAC_KEY" in result.stderr


def test_bad_drop_path_exit_code_1(cli_env, tmp_path):
    result = run_cli(cli_env, "ingest", str(tmp_path / "missing"))
    assert result.returncode == 1 and "not found" in result.stderr
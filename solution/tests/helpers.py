"""Small builders/readers shared by the tests."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

MOTOR = "motor_renewal"
FEEDBACK = "policy_feedback"
MOTOR_HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


def event(event_id: str, event_type: str, call_id: str, occurred_at: str, phone: str | None = None,
          workflow_id: str = MOTOR, **payload: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"event_id": event_id, "event_type": event_type, "workflow_id": workflow_id,
                            "call_id": call_id, "occurred_at": occurred_at, "payload": payload}
    if phone is not None:
        data["phone"] = phone
    return data


def write_drop(root: Path, name: str, leads: dict[str, dict[str, str]] | None = None,
               events: dict[str, list[Any]] | None = None) -> Path:
    """leads = {workflow: {"batch_001.csv": text}}, events = {"events_0001.jsonl": [dict | raw str]}."""
    drop = root / name
    for workflow, files in (leads or {}).items():
        (drop / "leads" / workflow).mkdir(parents=True, exist_ok=True)
        for filename, text in files.items():
            (drop / "leads" / workflow / filename).write_text(text, encoding="utf-8", newline="")
    for filename, lines in (events or {}).items():
        (drop / "events").mkdir(parents=True, exist_ok=True)
        body = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n"
        (drop / "events" / filename).write_text(body, encoding="utf-8")
    return drop


def motor_csv(*rows: str, header: str = MOTOR_HEADER) -> str:
    return "\r\n".join([header, *rows]) + "\r\n"


def read_reports(out_dir: Path) -> dict[str, Any]:
    """Read the five report files into the same shape as ``reference_model.run_reference``."""
    result: dict[str, Any] = {}
    for name in ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv"):
        with (out_dir / name).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        result[name] = rows[1:]
        result[name + ":header"] = rows[0]
    result["data_quality.json"] = json.loads((out_dir / "data_quality.json").read_text(encoding="utf-8"))
    return result

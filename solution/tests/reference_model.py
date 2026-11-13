"""Independent reference model used as a test oracle.

It recomputes every report *directly from the raw drop files in one global pass*: no database, no
incremental processing, no quarantine table, and its own small parsers (``strptime``, plain regexes).
It is NOT an official expected output (the starter repo ships none); it is a second implementation of
the README rules written differently from the pipeline, so disagreements point at bugs in one of them.
It shares only my reading of the README, so a misreading of the spec would not be caught here.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
OVERRIDE = "disposition.override"
SET = "disposition.set"
EVENT_TYPES = {"call.initiated", "call.telephony_failed", "call.connected", "call.ended", SET, OVERRIDE}


def _header(text: str) -> str:
    return re.sub(r"[_\s]+", " ", text.strip().lower())


def _phone(text: str) -> str | None:
    digits = "".join(ch for ch in text if ch in "0123456789")
    if len(digits) == 12 and digits[:2] == "91":
        digits = digits[2:]
    elif len(digits) == 11 and digits[0] == "0":
        digits = digits[1:]
    return "+91" + digits if len(digits) == 10 and digits[0] in "6789" else None


def _date_ok(text: str) -> bool:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            pass
    return False


def _decimal_ok(text: str) -> bool:
    m = re.fullmatch(r"(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    if not m:
        return False
    try:
        Decimal(m.group(1).replace(",", ""))
        return True
    except InvalidOperation:
        return False


def _ts(text: str) -> datetime:
    text = text.strip()
    if text.endswith(("Z", "z")):
        return datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def _load_schemas(schemas_dir: Path) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    schemas = {}
    for path in sorted(schemas_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        schemas[data["workflow_id"]] = data
    dispositions: dict[str, dict[str, dict]] = defaultdict(dict)
    with (schemas_dir / "dispositions.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            dispositions[row["workflow_id"]][row["code"]] = {
                "terminal": row["is_terminal"].strip().lower() == "true",
                "success": row["is_success"].strip().lower() == "true",
            }
    return schemas, dispositions


def run_reference(drop_dirs: list[Path], schemas_dir: Path) -> dict[str, Any]:
    schemas, dispositions = _load_schemas(schemas_dir)
    quality: dict[str, Any] = Counter()
    reasons: Counter[str] = Counter()
    rejections: list[tuple[str, str, int, str]] = []
    leads: dict[tuple[str, str], dict[str, Any]] = {}

    # ---------------------------------------------------------------- leads (all drops, in order)
    for drop in drop_dirs:
        leads_root = drop / "leads"
        for wf_dir in sorted(p for p in leads_root.iterdir() if p.is_dir()):
            schema = schemas[wf_dir.name]
            lookup = {}
            for f in schema["fields"]:
                for label in [f["name"], *f.get("aliases", [])]:
                    lookup[_header(label)] = f["name"]
            for csv_path in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
                with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                    rows = list(csv.reader(handle))
                rows = [r for r in rows]
                header_idx = next(i for i, r in enumerate(rows) if any(c.strip() for c in r))
                columns: dict[str, int] = {}
                for i, cell in enumerate(rows[header_idx]):
                    name = lookup.get(_header(cell))
                    if name and name not in columns:
                        columns[name] = i
                row_number = 0
                for cells in rows[header_idx + 1:]:
                    if not any(c.strip() for c in cells):
                        continue
                    row_number += 1
                    quality["lead_rows_total"] += 1
                    raw = {n: (cells[i].strip() if i < len(cells) else "") or None for n, i in columns.items()}
                    reason, warnings, values = None, 0, {}
                    for f in schema["fields"]:
                        v = raw.get(f["name"])
                        if v is None:
                            if f.get("required"):
                                reason = f"missing_required:{f['name']}"
                                break
                            continue
                        ok = True
                        if f["type"] == "phone":
                            v = _phone(v)
                            ok = v is not None
                        elif f["type"] == "date":
                            ok = _date_ok(v)
                        elif f["type"] == "decimal":
                            ok = _decimal_ok(v)
                        elif f["type"] == "enum":
                            ok = v.lower() in [x.lower() for x in f["values"]]
                        if not ok:
                            if f.get("required"):
                                reason = f"invalid_{f['type']}"
                                break
                            warnings += 1
                            continue
                        values[f["name"]] = v
                    if reason:
                        quality["lead_rows_rejected"] += 1
                        reasons[reason] += 1
                        rejections.append((wf_dir.name, csv_path.stem, row_number, reason))
                        continue
                    quality["field_warnings"] += warnings
                    ref_name = next(f["name"] for f in schema["fields"] if f.get("reference"))
                    phone_name = next(f["name"] for f in schema["fields"] if f["type"] == "phone")
                    key = (wf_dir.name, values[phone_name])
                    if key in leads:
                        quality["duplicate_lead_rows_merged"] += 1
                        leads[key]["ref"] = values[ref_name]
                    else:
                        leads[key] = {"batch": csv_path.stem, "ref": values[ref_name], "order": len(leads)}

    # ---------------------------------------------------------------- events (all drops, in order)
    seen: set[str] = set()
    events: list[dict[str, Any]] = []
    for drop in drop_dirs:
        for path in sorted((drop / "events").glob("*.jsonl"), key=lambda p: p.name):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                quality["events_total"] += 1
                try:
                    data = json.loads(line)
                    event_id = data["event_id"]
                    assert isinstance(event_id, str) and event_id
                except (ValueError, KeyError, TypeError, AssertionError):
                    quality["malformed_events"] += 1
                    continue
                if event_id in seen:
                    quality["duplicate_events_dropped"] += 1
                    continue
                seen.add(event_id)
                events.append(data)

    # linking over the complete event set
    by_call: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    linked: dict[str, tuple[str, str] | None] = {}
    for e in events:
        if e["event_type"] == OVERRIDE:
            continue
        phone = _phone(e["phone"]) if e.get("phone") else None
        key = (e["workflow_id"], phone) if phone and (e["workflow_id"], phone) in leads else None
        linked[e["event_id"]] = key
        if key:
            by_call[(e["workflow_id"], e["call_id"])].add(key)
    for e in events:
        if e["event_type"] == OVERRIDE:
            candidates = by_call.get((e["workflow_id"], e["call_id"]), set())
            linked[e["event_id"]] = next(iter(candidates)) if len(candidates) == 1 else None

    valid: list[dict[str, Any]] = []
    for e in events:
        key = linked[e["event_id"]]
        if key is None:
            quality["orphan_events"] += 1
            continue
        if e["event_type"] in (SET, OVERRIDE) and e["payload"].get("disposition_code") not in dispositions[e["workflow_id"]]:
            quality["unknown_disposition_events"] += 1
            continue
        valid.append({**e, "lead": key, "ts": _ts(e["occurred_at"])})

    # ---------------------------------------------------------------- derived facts
    per_call: dict[tuple[tuple[str, str], str], list[dict[str, Any]]] = defaultdict(list)
    for e in valid:
        per_call[(e["lead"], e["call_id"])].append(e)
    lead_calls: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (lead, call_id), evs in per_call.items():
        init = [e for e in evs if e["event_type"] == "call.initiated"]
        if not init:
            continue
        ovr = sorted((e for e in evs if e["event_type"] == OVERRIDE), key=lambda e: (e["ts"], e["event_id"]))
        sets = sorted((e for e in evs if e["event_type"] == SET), key=lambda e: (e["ts"], e["event_id"]))
        eff = (ovr or sets)[-1]["payload"]["disposition_code"] if (ovr or sets) else None
        lead_calls[lead].append({
            "call_id": call_id, "at": min(e["ts"] for e in init), "eff": eff,
            "connected": any(e["event_type"] == "call.connected" for e in evs),
            "failed": any(e["event_type"] == "call.telephony_failed" for e in evs),
        })

    batch: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    daily: dict[tuple[str, str], dict[str, Any]] = {}
    lead_rows = []
    for (wf, phone), info in leads.items():
        calls = lead_calls.get((wf, phone), [])
        disp = dispositions[wf]
        if not calls:
            bucket = "yet_to_reach_out"
        elif any(c["eff"] and disp[c["eff"]]["terminal"] for c in calls):
            bucket = "completed"
        elif all(c["failed"] for c in calls):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        batch[(wf, info["batch"])]["total_leads"] += 1
        batch[(wf, info["batch"])][bucket] += 1
        for c in calls:
            day = daily.setdefault((wf, c["at"].astimezone(IST).date().isoformat()), {"t": 0, "c": 0, "s": set()})
            day["t"] += 1
            day["c"] += c["connected"]
            if c["eff"] and disp[c["eff"]]["success"]:
                day["s"].add((wf, phone))
        with_eff = sorted((c for c in calls if c["eff"]), key=lambda c: (c["at"], c["call_id"]))
        lead_rows.append((wf, info["ref"], info["order"], [
            wf, info["ref"], info["batch"], "+91******" + phone[-4:], bucket, len(calls),
            sum(c["connected"] for c in calls),
            max(c["at"] for c in calls).astimezone(IST).isoformat(timespec="seconds") if calls else "",
            with_eff[-1]["eff"] if with_eff else "",
            "true" if any(c["eff"] and disp[c["eff"]]["success"] for c in calls) else "false",
        ]))

    cols = ["total_leads", "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
    return {
        "batch_summary.csv": [[wf, b, *[str(c[k]) for k in cols]] for (wf, b), c in sorted(batch.items())],
        "daily_overview.csv": [
            [wf, d, str(v["t"]), str(v["c"]), f"{v['c'] / v['t']:.4f}", str(len(v["s"]))]
            for (wf, d), v in sorted(daily.items())
        ],
        "lead_report.csv": [[str(x) for x in r[3]] for r in sorted(lead_rows, key=lambda r: (r[0], r[1], r[2]))],
        "rejections.csv": [[w, b, str(n), r] for w, b, n, r in sorted(rejections)],
        "data_quality.json": {
            "lead_rows_total": quality["lead_rows_total"],
            "lead_rows_rejected": quality["lead_rows_rejected"],
            "rejections_by_reason": dict(sorted(reasons.items())),
            "duplicate_lead_rows_merged": quality["duplicate_lead_rows_merged"],
            "field_warnings": quality["field_warnings"],
            "events_total": quality["events_total"],
            "malformed_events": quality["malformed_events"],
            "duplicate_events_dropped": quality["duplicate_events_dropped"],
            "orphan_events": quality["orphan_events"],
            "unknown_disposition_events": quality["unknown_disposition_events"],
        },
    }

# Submission

## Candidate
- **Name:** Anjali
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 4-5 hours

## How to run
```bash
docker compose up -d                                   # Postgres 16 (user au / au_local_only / db au_assignment)
python3 -m venv .venv && source .venv/bin/activate
pip install -r solution/requirements.txt
cd solution
cp .env.example .env                                   # then set PII_ENCRYPTION_KEY and PII_HMAC_KEY (commands inside)
python -m src.main migrate
python ../assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m src.main ingest data/drop_1
python -m src.main ingest data/drop_2
python -m src.main report --out reports/
python -m pytest                                       # needs DATABASE_URL (CREATE DATABASE rights)
```
Details: `solution/README.md`. I added `solution/requirements.txt` instead of editing the root `requirements.txt`
(which lists Assignment B's packages).

## Design

**Flow.** `ingest <drop>` runs in **one transaction** (guarded by a Postgres advisory lock): lead CSVs (workflow dirs
in name order, files in filename order, rows in file order) -> event JSONL files (filename order) -> orphan resolution.
`report` derives all five files from committed state in one `REPEATABLE READ` snapshot. Business rules are pure
modules (`normalizers`, `timestamps`, `lead_parsing`, `event_parsing`, `derived`) with no DB access; the DB modules
only orchestrate. Schemas and dispositions are read from the supplied files; no field name, workflow or disposition
code is hard-coded (only the six event types fixed by the spec).

**Tables** (`solution/src/models.py`, created by the Alembic migration `0001`):

| Table | Purpose / key constraints |
|---|---|
| `workflows`, `dispositions` | Reference data synced from the schema files. `dispositions` PK `(workflow_id, code)`. |
| `drops` | One row per delivered drop, unique `name`; `id` is the ingest order. |
| `source_files` | One row per ingested CSV/JSONL: unique `(drop_id, rel_path)`, SHA-256, per-file counters. A recorded file is never processed again. |
| `leads` | Unique `(workflow_id, phone_hash)`; permanent `batch_id`; plaintext `external_ref` (schema `reference` field); `pii_encrypted` (Fernet blob of every `pii:true` field); `attributes` JSONB (other fields; schema-driven so no per-workflow columns). |
| `lead_rejections` | `(source_file_id, row_number)` unique, `reason`. Stores no row values, so no PII. |
| `event_registry` | **PK `event_id`**: every event id ever seen. The database-level guarantee behind global de-duplication. |
| `interaction_events` | **Append-only ledger**: only linked events with a known disposition. FK to `event_registry` and `leads`; `UPDATE`, `DELETE` and `TRUNCATE` are blocked by triggers. Keeps the original timestamp text and payload (no phone). |
| `quarantined_events` | Events that are not reportable: `orphan` (not linked yet), `unknown_disposition` (linked, code not in catalogue, final) and `promoted` (was an orphan, later linked and validated; a ledger copy exists). Keeps only `phone_hash`, never the phone. A CHECK ties `lead_id` to the status. |

**PII.** `pii:true` fields (and the identity phone even if a schema forgot the flag) are stored only inside a Fernet
token (AES-128-CBC + HMAC-SHA256, authenticated; `MultiFernet` supports key rotation). Lookup uses
`HMAC-SHA256(PII_HMAC_KEY, workflow_id || 0x00 || canonical_phone)`: deterministic so leads and events can be matched,
but keyed (no rainbow table over the 4-billion number space) and workflow-scoped (the same number in two workflows
never shares an identity). Event phones are hashed and discarded. Phones are decrypted only in `report`, masked
immediately. Keys come from environment variables; nothing is logged except counts, file names and row numbers.

**Idempotency and incremental processing.** A file is identified by `(drop directory name, relative path)` plus its
SHA-256. Re-ingesting skips recorded files, so lead/event counters (stored per file and summed at report time) cannot
double count; a recorded path whose content changed is a hard error (refuses silently different data). Since a drop
commits atomically, a crash leaves nothing partial and a re-run simply redoes it. Counters for orphans and unknown
dispositions are not accumulated at all: they are `COUNT(*)` over the current quarantine state, which is why they
reflect the final state. Processing is incremental in the sense that drop 2 does not reprocess drop 1's files; only
orphan resolution looks back at quarantined events. Trade-off: results depend on **ingest order** (last write wins on
merges, first valid row owns `batch_id`), and one transaction per drop holds a lock for the whole drop.

**Orphan recovery.** After every drop `linking.resolve_quarantine` (idempotent) (1) links orphans with a phone hash to
the lead with the same `(workflow_id, hash)`, (2) links each orphan `disposition.override` through its
`(workflow_id, call_id)` to the single lead that the call's other linked events belong to. A linked event is
promoted into the ledger only if its disposition code is known; a linked event with an unknown code becomes
`unknown_disposition` and is **never** added to the ledger, however late its lead arrives. Order of checks per the
spec: malformed, duplicate, orphan, unknown disposition, so an unlinked event with an unknown code counts as an orphan
until it links.

**Effective disposition and calls.** Calls are grouped by `(lead, call_id)` and count only if a linked
`call.initiated` exists; date/time come from it (IST). Effective disposition = latest override by `occurred_at`, else
latest set, else none; the ledger rows are never edited. Bucket order is exactly the spec's. Sorting is done in Python
by code point, not by database collation.

## Decisions & assumptions

Requirements that the spec states explicitly were implemented literally. Rows below are only where the spec is silent
or ambiguous (assumptions) or where I went beyond it (extension).

| Decision | Why | Alternative considered |
|---|---|---|
| Dates: day and month must be 2 digits, year 4 (`1/2/2026` is invalid) | The spec lists `DD/MM/YYYY` etc. | Lenient 1-2 digits (`strptime` style) |
| Decimal rounding is half-up; accepted shape is digits with comma groups and optional fraction; `NaN`/`1e5`/`-5` invalid | "Round to 2 places", "must be >= 0"; Python `Decimal` would otherwise accept `NaN` | Banker's rounding |
| `connectivity_rate` uses Python `f"{c/t:.4f}"` | Matches the most likely reference implementation; differs from half-up only on exact binary ties | Decimal half-up |
| Header normalisation order is lower, trim, collapse (as written) | Literal reading | Trim after collapsing |
| Two columns matching the same field: first column wins | Spec silent | Last wins / reject file |
| Required non-phone field present but invalid: reason `invalid_<type>` | Spec defines only `invalid_phone`; unreachable with the current schemas | Treat as missing |
| **Extension:** event with an `event_id` but unknown `event_type`, or missing `workflow_id`/`call_id`, or unparseable `occurred_at` counts as malformed; its id is still registered so later copies are duplicates | Spec's malformed definition is only "bad JSON or no event_id", but such events cannot be processed; the generator never produces them | Crash; orphan |
| Non-override event with no usable phone is an orphan; an override links **only** through `call_id` | Literal spec | Fall back to `call_id` for all events |
| Override whose call's events belong to two different leads stays an orphan | Never guess | Pick one |
| Equal `occurred_at`: higher `event_id` wins; several `call.initiated` for one call: earliest | Determinism | - |
| Events sharing a `call_id` but linked to different leads are different calls | Simple, and consistent with "a call needs its own initiated event" | Group by call_id only |
| Disposition code match is exact (case-sensitive) | Spec says "not in `dispositions.csv`" | Case-insensitive |
| Whitespace-only event lines are treated as empty (not counted) | "non-empty line" | Count them as malformed |
| Defaults are applied when a lead is created | Equivalent to "after merging": a later non-null value always overwrites a default and null never overwrites | Second pass at the end |
| `external_ref` can change if a later duplicate carries a different reference value | Spec: non-null overwrites (last write wins) | Freeze the first value |
| `rejections_by_reason` lists only reasons that occurred; `batch_summary` lists only batches that own at least one lead | Spec template shows zeros as placeholders | Zero-filled keys / rows |
| Drop identity = directory name; ingest order = invocation order | The spec says "order ingested" | Alphabetical |
| Unknown workflow folder, unreadable CSV (bad UTF-8) or a changed already-ingested file aborts the drop with a clear error | Never silently lose data | Skip and warn |

## Failure modes considered
- **Crash mid-drop:** whole drop is one transaction, so it rolls back, including counters (tested by injecting a failure).
- **Re-run / overlapping runs:** file records and the advisory lock make a re-run a no-op; two simultaneous ingests are serialised (tested with two threads).
- **Bad rows/lines:** rejections, warnings, malformed lines, duplicates, orphans and unknown dispositions are counted, not fatal. Unreadable CSV is a clear error.
- **Events before leads / overrides before their call:** quarantined and re-resolved after every drop.
- **Tampering/mistakes:** database triggers block ledger updates; unique/FK/CHECK constraints block duplicate leads, duplicate event ids and inconsistent quarantine rows (tested).
- **Not handled / limitations:** the event `payload` is stored as received (if a feed put PII there it would be stored; the spec's payloads contain none). Rotating `PII_HMAC_KEY` needs a re-hash job (encryption keys rotate via MultiFernet). A drop is loaded in one transaction (fine for the supplied sizes, would need chunked commits for much larger drops). Reports are computed in Python after loading the ledger, not in SQL. Late-added files that sort before already-ingested files would break the file-order assumption. No authentication or per-user access control; no retention policy.

## Testing
219 pytest tests, all passing in my last full run (see Results for the environment).
- **Unit (no DB):** every phone format and invalid phone, all date formats and impossible dates, currency formats, enum, headers, timestamps (UTC/offset/naive, IST date rollover), row validation order, effective disposition, bucket priority, encryption/HMAC.
- **Database integration (real PostgreSQL, one temporary database per test):** merging, permanent `batch_id`, defaults, blank rows, renamed/reordered columns, rejections without PII, event dedupe incl. across drops, malformed lines, orphan recovery, override linking, unknown dispositions never promoted, rollback, concurrency, append-only triggers, constraints, report headers/sorting/formats/calculations, CLI.
- **End to end, 4 seeds (42, 7, 2026, 12345):** after `drop_1` and after `drop_2` the reports equal an independent single-pass reference model (`tests/reference_model.py`); re-ingest leaves the database byte-identical; a clean database ingesting both drops gives identical reports; no customer name or phone number appears in any table or report file.
- **Mutation check:** I deliberately broke 8 rules (bucket order, override precedence, call_id linking, unknown-disposition promotion, distinct success leads, null-overwrite on merge, file skipping, naive-as-UTC); the suite failed each time.
- **Not done:** there is **no expected-output reference in the repo**, so I cannot claim the reports match the graders' expected files. The reference model is my own second implementation of my reading of the spec: it catches implementation bugs, not a shared misreading. The tests ran against a PostgreSQL 16.15 installed locally (not via `docker compose`, which was not available in my sandbox); please run `docker compose up -d` and `pytest` once on your machine.

## Results
Environment: Linux sandbox, PostgreSQL 16.15, Python 3.12.3, single run each (not benchmarked).
- **Small, seed 42:** 2057 lead rows (108 rejected, 57 duplicates merged, 122 warnings), 8745 event lines (8 malformed, 237 duplicates, 462 orphans and 18 unknown dispositions remain at the end), 1892 leads. After `drop_1` alone many more events are orphans; `drop_2` resolves 902 by phone and 54 overrides by `call_id`.
- **Large, seed 42** (632,602 event lines, ~144k CSV lines): `ingest drop_1` 82 s, `ingest drop_2` 109 s, re-ingesting both 0.8 s each, `report` 9 s. Output equalled the reference model on all five files (132,903 leads). **This misses the optional "under 2 minutes" stretch goal** (about 191 s ingest in total). I did not profile it; per-event hashing and row-by-row Python parsing are the likely costs.
- **Surprising in the data:** the generator's unknown disposition codes are variants and typos of real ones (`RENEWD`, `call_back`, `unknown`), so exact matching matters; many events for leads uploaded in `drop_2` already appear in `drop_1` and stay orphans until then; some orphans never resolve (their lead row was rejected or the event's phone is invalid).

## If I had another day
1. Run on the real `docker compose` Postgres and fix any difference; ask for/obtain expected output to diff against.
2. Profile and speed up event ingestion (bulk hashing, COPY) to meet the 2-minute stretch goal.
3. Move report aggregation into SQL for scale; chunked commits per file for very large drops.
4. Re-hash tooling for HMAC key rotation; optional encrypted raw-event archive for audit.
5. A short production note: schedule `ingest` per drop arrival, alert on non-zero exit, keep `source_files` as the audit trail, back-fill by re-running with a fresh database.

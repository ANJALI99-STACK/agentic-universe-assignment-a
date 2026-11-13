"""Lead ingestion: validate rows, reject bad ones, create leads and merge duplicates.

State is kept per file in memory and flushed with bulk statements at the end of each file;
everything runs inside the caller's transaction, so a failure rolls the whole drop back.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, func, insert, select, update
from sqlalchemy.orm import Session

from src.errors import IngestError
from src.lead_parsing import iter_raw_rows, validate_row
from src.logging_setup import log_kv
from src.models import Lead, LeadRejection
from src.schemas import Catalogue, WorkflowSchema
from src.security import PiiProtector

logger = logging.getLogger(__name__)


@dataclass
class LeadFileStats:
    lead_rows_total: int = 0
    lead_rows_rejected: int = 0
    duplicate_lead_rows_merged: int = 0
    field_warnings: int = 0


@dataclass
class LeadState:
    """In-memory view of one lead while a file is being processed (contains PII: never log)."""

    workflow_id: str
    phone_hash: str
    external_ref: str
    batch_id: str
    source_file_id: int
    pii: dict[str, str | None] = field(repr=False)
    attributes: dict[str, str | None]
    lead_id: int | None = None  # None => not yet inserted
    dirty: bool = False

    def merge(self, schema: WorkflowSchema, values: dict[str, str | None]) -> None:
        """Overwrite only fields whose incoming value is non-null (last write wins)."""
        encrypted = schema.encrypted_field_names
        reference = schema.reference_field.name
        for name, value in values.items():
            if value is None:
                continue
            if name == reference:
                changed, self.external_ref = self.external_ref != value, value
            elif name in encrypted:
                changed, self.pii[name] = self.pii.get(name) != value, value
            else:
                changed, self.attributes[name] = self.attributes.get(name) != value, value
            self.dirty = self.dirty or changed


def _new_state(
    schema: WorkflowSchema, values: dict[str, str | None], phone_hash: str, batch_id: str, source_file_id: int
) -> LeadState:
    encrypted = schema.encrypted_field_names
    reference = schema.reference_field.name
    pii: dict[str, str | None] = {}
    attributes: dict[str, str | None] = {}
    for spec in schema.fields:
        if spec.name == reference:
            continue
        value = values.get(spec.name)
        if value is None and spec.default is not None:
            # Equivalent to "apply defaults after merging": a later non-null value overwrites the
            # default, and a later null never overwrites anything.
            value = spec.default
        (pii if spec.name in encrypted else attributes)[spec.name] = value
    return LeadState(
        schema.workflow_id, phone_hash, values[reference] or "", batch_id, source_file_id, pii, attributes
    )


class LeadIngestor:
    """Ingests lead CSV files for one drop; holds an in-memory index of existing lead ids."""

    def __init__(self, session: Session, catalogue: Catalogue, protector: PiiProtector) -> None:
        self._session = session
        self._catalogue = catalogue
        self._protector = protector
        self.lead_index: dict[tuple[str, str], int] = {
            (wf, h): lead_id
            for wf, h, lead_id in session.execute(select(Lead.workflow_id, Lead.phone_hash, Lead.id))
        }

    def ingest_file(self, source_file_id: int, path: Path, workflow_id: str, batch_id: str) -> LeadFileStats:
        schema = self._catalogue.workflows[workflow_id]
        phone_field = schema.identity_phone_field.name
        stats = LeadFileStats()
        states: dict[str, LeadState] = {}
        rejections: list[dict[str, Any]] = []

        try:
            for row_number, raw in iter_raw_rows(path, schema):
                stats.lead_rows_total += 1
                result = validate_row(schema, row_number, raw)
                if result.rejected:
                    stats.lead_rows_rejected += 1
                    rejections.append({
                        "source_file_id": source_file_id, "workflow_id": workflow_id, "batch_id": batch_id,
                        "row_number": row_number, "reason": result.rejection_reason,
                    })
                    continue
                stats.field_warnings += result.warnings
                phone_hash = self._protector.phone_hash(workflow_id, result.values[phone_field] or "")
                state = states.get(phone_hash)
                if state is None:
                    existing_id = self.lead_index.get((workflow_id, phone_hash))
                    if existing_id is None:  # first valid row for this identity creates the lead
                        states[phone_hash] = _new_state(schema, result.values, phone_hash, batch_id, source_file_id)
                        continue
                    state = self._load_state(schema, existing_id, phone_hash)
                    states[phone_hash] = state
                state.merge(schema, result.values)
                stats.duplicate_lead_rows_merged += 1

        except (UnicodeDecodeError, csv.Error) as exc:  # unreadable file: abort the drop, log no content
            raise IngestError(f"cannot read lead file {path.name}: {type(exc).__name__}") from exc

        self._flush(states)
        if rejections:
            self._session.execute(insert(LeadRejection), rejections)
        log_kv(logger, "lead_file_ingested", file=path.name, workflow_id=workflow_id, batch_id=batch_id,
               rows=stats.lead_rows_total, rejected=stats.lead_rows_rejected,
               merged=stats.duplicate_lead_rows_merged, warnings=stats.field_warnings)
        return stats

    # ------------------------------------------------------------------ persistence
    def _load_state(self, schema: WorkflowSchema, lead_id: int, phone_hash: str) -> LeadState:
        row = self._session.execute(
            select(Lead.external_ref, Lead.batch_id, Lead.source_file_id, Lead.pii_encrypted, Lead.attributes)
            .where(Lead.id == lead_id)
        ).one()
        return LeadState(
            schema.workflow_id, phone_hash, row.external_ref, row.batch_id, row.source_file_id,
            self._protector.decrypt_json(row.pii_encrypted), dict(row.attributes), lead_id=lead_id,
        )

    def _flush(self, states: dict[str, LeadState]) -> None:
        new = [s for s in states.values() if s.lead_id is None]
        changed = [s for s in states.values() if s.lead_id is not None and s.dirty]
        if new:
            rows = [
                {
                    "workflow_id": s.workflow_id, "phone_hash": s.phone_hash, "external_ref": s.external_ref,
                    "batch_id": s.batch_id, "source_file_id": s.source_file_id,
                    "pii_encrypted": self._protector.encrypt_json(s.pii), "attributes": s.attributes,
                }
                for s in new
            ]
            returned = self._session.execute(insert(Lead).returning(Lead.workflow_id, Lead.phone_hash, Lead.id), rows)
            for workflow_id, phone_hash, lead_id in returned:
                self.lead_index[(workflow_id, phone_hash)] = lead_id
        if changed:
            statement = (
                update(Lead.__table__)
                .where(Lead.__table__.c.id == bindparam("b_id"))
                .values(
                    external_ref=bindparam("b_ref"), pii_encrypted=bindparam("b_pii"),
                    attributes=bindparam("b_attrs"), updated_at=func.now(),
                )
            )
            self._session.execute(statement, [
                {"b_id": s.lead_id, "b_ref": s.external_ref, "b_pii": self._protector.encrypt_json(s.pii),
                 "b_attrs": s.attributes}
                for s in changed
            ])

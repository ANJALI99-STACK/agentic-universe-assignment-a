"""Relational model.

Table purposes
--------------
workflows / dispositions   Reference data synced from the schema files (FK targets).
drops                      One row per delivered drop (``drop_1``...), identity = directory name.
source_files               One row per ingested lead CSV / events JSONL, with its content hash and
                           per-file counters. A file recorded here is never processed again.
leads                      One row per (workflow_id, phone). PII is only stored encrypted.
lead_rejections            Rejected CSV rows: file, row number, reason. No row values, so no PII.
event_registry             Every event_id ever seen (global de-duplication, DB-enforced).
interaction_events         The append-only ledger: valid, linked events with a known disposition.
quarantined_events         Events that are NOT (yet) reportable: orphans awaiting a lead, and linked
                           events whose disposition code is unknown. Mutable working area.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.constants import EVENT_TYPES, STATUS_ORPHAN, STATUS_PROMOTED, STATUS_UNKNOWN_DISPOSITION


class Base(DeclarativeBase):
    pass


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _zero() -> Any:
    return text("0")


class Workflow(Base):
    __tablename__ = "workflows"
    workflow_id: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class DispositionCode(Base):
    __tablename__ = "dispositions"
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id"), primary_key=True)
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False)


class Drop(Base):
    __tablename__ = "drops"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # also the ingest order
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.workflow_id"))
    batch_id: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # lead-file counters
    lead_rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    lead_rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    duplicate_lead_rows_merged: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    field_warnings: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    # event-file counters
    events_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    malformed_events: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    duplicate_events_dropped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=_zero())
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("drop_id", "rel_path", name="uq_source_files_drop_path"),
        CheckConstraint("kind IN ('leads', 'events')", name="ck_source_files_kind"),
        CheckConstraint(
            "(kind = 'leads' AND workflow_id IS NOT NULL AND batch_id IS NOT NULL) OR kind = 'events'",
            name="ck_source_files_lead_has_workflow",
        ),
    )


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id"), nullable=False)
    phone_hash: Mapped[str] = mapped_column(Text, nullable=False)  # HMAC-SHA256 hex(workflow_id, phone)
    external_ref: Mapped[str] = mapped_column(Text, nullable=False)  # schema field with reference=true
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)  # permanent: stem of the creating file
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    pii_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token of all pii=true fields
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # non-PII, non-reference fields
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workflow_id", "phone_hash", name="uq_leads_workflow_phone"),
        CheckConstraint("char_length(phone_hash) = 64", name="ck_leads_phone_hash_len"),
        Index("ix_leads_workflow_batch", "workflow_id", "batch_id"),
    )


class LeadRejection(Base):
    __tablename__ = "lead_rejections"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id"), nullable=False)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_file_id", "row_number", name="uq_lead_rejections_file_row"),
        CheckConstraint("row_number >= 1", name="ck_lead_rejections_row_positive"),
    )


class EventRegistry(Base):
    """Every event_id ever seen. The primary key is the global de-duplication guarantee."""

    __tablename__ = "event_registry"
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)


class InteractionEvent(Base):
    """Append-only ledger (UPDATE / DELETE / TRUNCATE are blocked by DB triggers)."""

    __tablename__ = "interaction_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event_registry.event_id"), nullable=False, unique=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.workflow_id"), nullable=False)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), nullable=False)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # UTC
    occurred_at_raw: Mapped[str] = mapped_column(Text, nullable=False)  # original text, for audit
    disposition_code: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # original payload (no phone)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in_list("event_type", EVENT_TYPES), name="ck_interaction_events_type"),
        Index("ix_interaction_events_lead_call", "lead_id", "call_id"),
        Index("ix_interaction_events_workflow_call", "workflow_id", "call_id"),
    )


class QuarantinedEvent(Base):
    """Events that must not appear in reports: orphans and unknown-disposition events.

    status ``orphan``              not linked to any lead yet (may be resolved by a later drop)
    status ``unknown_disposition`` linked, but the disposition code is not in the catalogue (final)
    status ``promoted``            was an orphan, got linked and validated; a copy now exists in the ledger
    No plaintext PII: only the keyed phone hash is kept for later linking.
    """

    __tablename__ = "quarantined_events"
    event_id: Mapped[str] = mapped_column(ForeignKey("event_registry.event_id"), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)  # no FK: may name an unknown workflow
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at_raw: Mapped[str] = mapped_column(Text, nullable=False)
    disposition_code: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    phone_hash: Mapped[str | None] = mapped_column(Text)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_files.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in_list("event_type", EVENT_TYPES), name="ck_quarantined_events_type"),
        CheckConstraint(
            _in_list("status", (STATUS_ORPHAN, STATUS_UNKNOWN_DISPOSITION, STATUS_PROMOTED)),
            name="ck_quarantined_events_status",
        ),
        CheckConstraint(
            "(status = 'orphan' AND lead_id IS NULL) OR (status <> 'orphan' AND lead_id IS NOT NULL)",
            name="ck_quarantined_events_lead_matches_status",
        ),
        Index("ix_quarantined_events_status", "status"),
        Index("ix_quarantined_events_phone", "workflow_id", "phone_hash", postgresql_where=text("status = 'orphan'")),
        Index("ix_quarantined_events_call", "workflow_id", "call_id"),
    )

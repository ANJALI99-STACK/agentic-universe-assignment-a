"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-12 21:22:52.010291
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('drops',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('workflows',
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.PrimaryKeyConstraint('workflow_id')
    )
    op.create_table('dispositions',
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('code', sa.Text(), nullable=False),
    sa.Column('label', sa.Text(), nullable=False),
    sa.Column('is_terminal', sa.Boolean(), nullable=False),
    sa.Column('is_success', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], ),
    sa.PrimaryKeyConstraint('workflow_id', 'code')
    )
    op.create_table('source_files',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('drop_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('rel_path', sa.Text(), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=True),
    sa.Column('batch_id', sa.Text(), nullable=True),
    sa.Column('sha256', sa.Text(), nullable=False),
    sa.Column('lead_rows_total', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('lead_rows_rejected', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('duplicate_lead_rows_merged', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('field_warnings', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('events_total', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('malformed_events', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('duplicate_events_dropped', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'leads' AND workflow_id IS NOT NULL AND batch_id IS NOT NULL) OR kind = 'events'", name='ck_source_files_lead_has_workflow'),
    sa.CheckConstraint("kind IN ('leads', 'events')", name='ck_source_files_kind'),
    sa.ForeignKeyConstraint(['drop_id'], ['drops.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('drop_id', 'rel_path', name='uq_source_files_drop_path')
    )
    op.create_table('event_registry',
    sa.Column('event_id', sa.Text(), nullable=False),
    sa.Column('source_file_id', sa.BigInteger(), nullable=False),
    sa.Column('line_number', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.id'], ),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_table('lead_rejections',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('source_file_id', sa.BigInteger(), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('row_number', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.CheckConstraint('row_number >= 1', name='ck_lead_rejections_row_positive'),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_file_id', 'row_number', name='uq_lead_rejections_file_row')
    )
    op.create_table('leads',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('phone_hash', sa.Text(), nullable=False),
    sa.Column('external_ref', sa.Text(), nullable=False),
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('source_file_id', sa.BigInteger(), nullable=False),
    sa.Column('pii_encrypted', sa.Text(), nullable=False),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('char_length(phone_hash) = 64', name='ck_leads_phone_hash_len'),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workflow_id', 'phone_hash', name='uq_leads_workflow_phone')
    )
    op.create_index('ix_leads_workflow_batch', 'leads', ['workflow_id', 'batch_id'], unique=False)
    op.create_table('interaction_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('event_id', sa.Text(), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('lead_id', sa.BigInteger(), nullable=False),
    sa.Column('call_id', sa.Text(), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('occurred_at_raw', sa.Text(), nullable=False),
    sa.Column('disposition_code', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_file_id', sa.BigInteger(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("event_type IN ('call.initiated', 'call.telephony_failed', 'call.connected', 'call.ended', 'disposition.set', 'disposition.override')", name='ck_interaction_events_type'),
    sa.ForeignKeyConstraint(['event_id'], ['event_registry.event_id'], ),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.id'], ),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id')
    )
    op.create_index('ix_interaction_events_lead_call', 'interaction_events', ['lead_id', 'call_id'], unique=False)
    op.create_index('ix_interaction_events_workflow_call', 'interaction_events', ['workflow_id', 'call_id'], unique=False)
    op.create_table('quarantined_events',
    sa.Column('event_id', sa.Text(), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('call_id', sa.Text(), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('occurred_at_raw', sa.Text(), nullable=False),
    sa.Column('disposition_code', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('phone_hash', sa.Text(), nullable=True),
    sa.Column('lead_id', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('source_file_id', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(status = 'orphan' AND lead_id IS NULL) OR (status <> 'orphan' AND lead_id IS NOT NULL)", name='ck_quarantined_events_lead_matches_status'),
    sa.CheckConstraint("event_type IN ('call.initiated', 'call.telephony_failed', 'call.connected', 'call.ended', 'disposition.set', 'disposition.override')", name='ck_quarantined_events_type'),
    sa.CheckConstraint("status IN ('orphan', 'unknown_disposition', 'promoted')", name='ck_quarantined_events_status'),
    sa.ForeignKeyConstraint(['event_id'], ['event_registry.event_id'], ),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ),
    sa.ForeignKeyConstraint(['source_file_id'], ['source_files.id'], ),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index('ix_quarantined_events_call', 'quarantined_events', ['workflow_id', 'call_id'], unique=False)
    op.create_index('ix_quarantined_events_phone', 'quarantined_events', ['workflow_id', 'phone_hash'], unique=False, postgresql_where=sa.text("status = 'orphan'"))
    op.create_index('ix_quarantined_events_status', 'quarantined_events', ['status'], unique=False)

    # The interaction ledger is append-only: block UPDATE / DELETE / TRUNCATE at the database level.
    op.execute("""
        CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'interaction_events is append-only (% not allowed)', TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER interaction_events_no_update_delete
        BEFORE UPDATE OR DELETE ON interaction_events
        FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation()
    """)
    op.execute("""
        CREATE TRIGGER interaction_events_no_truncate
        BEFORE TRUNCATE ON interaction_events
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS interaction_events_no_truncate ON interaction_events")
    op.execute("DROP TRIGGER IF EXISTS interaction_events_no_update_delete ON interaction_events")
    op.execute("DROP FUNCTION IF EXISTS forbid_ledger_mutation()")
    op.drop_index('ix_quarantined_events_status', table_name='quarantined_events')
    op.drop_index('ix_quarantined_events_phone', table_name='quarantined_events', postgresql_where=sa.text("status = 'orphan'"))
    op.drop_index('ix_quarantined_events_call', table_name='quarantined_events')
    op.drop_table('quarantined_events')
    op.drop_index('ix_interaction_events_workflow_call', table_name='interaction_events')
    op.drop_index('ix_interaction_events_lead_call', table_name='interaction_events')
    op.drop_table('interaction_events')
    op.drop_index('ix_leads_workflow_batch', table_name='leads')
    op.drop_table('leads')
    op.drop_table('lead_rejections')
    op.drop_table('event_registry')
    op.drop_table('source_files')
    op.drop_table('dispositions')
    op.drop_table('workflows')
    op.drop_table('drops')

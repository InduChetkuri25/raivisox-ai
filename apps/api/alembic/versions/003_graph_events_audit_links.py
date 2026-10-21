"""V3 Phase 1: append-only graph events + audit provenance side table

Two NEW tables and no alterations to anything existing (design §13 Phase 1:
"additive table + indexes; no changes to existing tables"):

- ``graph_events``      — the provenance record (services/graph/events.py)
- ``audit_links``       — provenance for audit rows, written in the same
                          transaction as the row it describes

``audit_events`` is deliberately untouched: it stays byte-identical for every
existing forensic reader, and legacy rows simply have no link row (their
provenance reads NULL rather than being invented).

Revision ID: 003
Revises: 002
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '003'
down_revision: Union[str, Sequence[str], None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'graph_events',
        sa.Column('seq', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('event_id', sa.String(36), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(48), nullable=False),
        sa.Column('occurred_at', sa.Float(), nullable=False),
        sa.Column('recorded_at', sa.Float(), nullable=False),
        sa.Column('actor_id', sa.String(100), nullable=False),
        sa.Column('actor_type', sa.String(32), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('run_id', sa.String(36), nullable=True),
        sa.Column('step_node_id', sa.String(64), nullable=True),
        sa.Column('tool_id', sa.String(64), nullable=True),
        sa.Column('action_id', sa.String(36), nullable=True),
        sa.Column('target_kind', sa.String(16), nullable=True),
        sa.Column('target_id', sa.String(512), nullable=True),
        sa.Column('outcome', sa.String(24), nullable=False),
        sa.Column('evidence_class', sa.String(16), nullable=False),
        sa.Column('source', sa.String(64), nullable=False),
        sa.Column('correlation_id', sa.String(100), nullable=True),
        sa.Column('idempotency_key', sa.String(200), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ['task_id'], ['tasks.id'], ondelete='CASCADE',
            name='fk_graph_events_task_id',
        ),
        sa.UniqueConstraint('event_id', name='uq_graph_events_event_id'),
        sa.UniqueConstraint('idempotency_key', name='uq_graph_events_idempotency_key'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_graph_events_user_seq', 'graph_events', ['user_id', 'seq'])
    op.create_index('ix_graph_events_task_seq', 'graph_events', ['task_id', 'seq'])
    op.create_index('ix_graph_events_run_seq', 'graph_events', ['run_id', 'seq'])
    op.create_index('ix_graph_events_action', 'graph_events', ['action_id'])
    op.create_index('ix_graph_events_type', 'graph_events', ['event_type'])

    op.create_table(
        'audit_links',
        sa.Column('event_id', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=True),
        sa.Column('task_id', sa.String(36), nullable=True),
        sa.Column('run_id', sa.String(36), nullable=True),
        sa.Column('step_node_id', sa.String(64), nullable=True),
        sa.Column('action_id', sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ['event_id'], ['audit_events.event_id'], ondelete='CASCADE',
            name='fk_audit_links_event_id',
        ),
        sa.PrimaryKeyConstraint('event_id', name='pk_audit_links'),
    )
    op.create_index('ix_audit_links_user_id', 'audit_links', ['user_id'])
    op.create_index('ix_audit_links_task_id', 'audit_links', ['task_id'])
    op.create_index('ix_audit_links_run_id', 'audit_links', ['run_id'])
    op.create_index('ix_audit_links_action_id', 'audit_links', ['action_id'])


def downgrade() -> None:
    op.drop_index('ix_audit_links_action_id', table_name='audit_links')
    op.drop_index('ix_audit_links_run_id', table_name='audit_links')
    op.drop_index('ix_audit_links_task_id', table_name='audit_links')
    op.drop_index('ix_audit_links_user_id', table_name='audit_links')
    op.drop_table('audit_links')

    op.drop_index('ix_graph_events_type', table_name='graph_events')
    op.drop_index('ix_graph_events_action', table_name='graph_events')
    op.drop_index('ix_graph_events_run_seq', table_name='graph_events')
    op.drop_index('ix_graph_events_task_seq', table_name='graph_events')
    op.drop_index('ix_graph_events_user_seq', table_name='graph_events')
    op.drop_table('graph_events')

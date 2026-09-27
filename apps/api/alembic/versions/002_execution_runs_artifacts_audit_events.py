"""Production hardening: runtime tables + schema drift fixes

Adds the three tables created so far only by startup create_all
(execution_runs, task_artifacts, audit_events), the two columns added
after the initial schema (tasks.outcome, task_events.execution_run_id),
and the indexes declared on the models but missing from 001.

Foreign keys are declared INLINE (create_table) — post-hoc
create_foreign_key() emits ALTER TABLE, which SQLite cannot apply.

Revision ID: 002
Revises: 001
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '002'
down_revision: Union[str, Sequence[str], None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Column drift vs the current models -------------------------------
    op.add_column('tasks', sa.Column('outcome', sa.String(40), nullable=True))
    op.create_index('ix_tasks_outcome', 'tasks', ['outcome'])

    op.add_column(
        'task_events', sa.Column('execution_run_id', sa.String(36), nullable=True)
    )
    op.create_index('ix_task_events_execution_run_id', 'task_events', ['execution_run_id'])

    # Indexes declared on the models but never created by 001.
    op.create_index('ix_recovery_attempts_step_id', 'recovery_attempts', ['step_id'])
    op.create_index('ix_tool_usage_step_id', 'tool_usage', ['step_id'])
    op.create_index('ix_verification_results_step_id', 'verification_results', ['step_id'])

    # --- execution_runs ---------------------------------------------------
    op.create_table(
        'execution_runs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('outcome', sa.String(40), nullable=True),
        sa.Column('error', sa.String(2000), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['task_id'], ['tasks.id'], ondelete='CASCADE',
            name='fk_execution_runs_task_id',
        ),
    )
    op.create_index('ix_execution_runs_task_id', 'execution_runs', ['task_id'])
    op.create_index(
        'ix_execution_runs_task_started', 'execution_runs', ['task_id', 'started_at']
    )

    # --- task_artifacts ---------------------------------------------------
    op.create_table(
        'task_artifacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        # No FK: step rows are recreated per run and artifact history must
        # survive them (matches the model).
        sa.Column('step_id', sa.String(36), nullable=True),
        sa.Column('node_id', sa.String(36), nullable=True),
        sa.Column('step_title', sa.String(500), nullable=True),
        sa.Column('run_id', sa.String(36), nullable=True),
        sa.Column('filename', sa.String(500), nullable=False),
        sa.Column('storage_path', sa.String(1024), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('content_type', sa.String(120), nullable=True),
        sa.Column('sha256', sa.String(64), nullable=True),
        sa.Column('is_current', sa.Boolean(), nullable=False),
        sa.Column('superseded_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['task_id'], ['tasks.id'], ondelete='CASCADE',
            name='fk_task_artifacts_task_id',
        ),
    )
    op.create_index('ix_task_artifacts_task_id', 'task_artifacts', ['task_id'])
    op.create_index('ix_task_artifacts_step_id', 'task_artifacts', ['step_id'])
    op.create_index('ix_task_artifacts_run_id', 'task_artifacts', ['run_id'])
    op.create_index('ix_task_artifacts_sha256', 'task_artifacts', ['sha256'])
    op.create_index('ix_task_artifacts_is_current', 'task_artifacts', ['is_current'])
    op.create_index(
        'ix_task_artifacts_task_current', 'task_artifacts', ['task_id', 'is_current']
    )
    op.create_index(
        'ix_task_artifacts_task_path', 'task_artifacts', ['task_id', 'storage_path']
    )

    # --- audit_events -----------------------------------------------------
    op.create_table(
        'audit_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('event_id', sa.String(64), nullable=False),
        sa.Column('actor_id', sa.String(100), nullable=False),
        sa.Column('action', sa.String(100), nullable=False),
        sa.Column('resource', sa.String(500), nullable=False),
        sa.Column('allowed', sa.Boolean(), nullable=False),
        sa.Column('request_id', sa.String(100), nullable=False),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('timestamp', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_audit_events_event_id', 'audit_events', ['event_id'], unique=True)
    op.create_index('ix_audit_events_actor_id', 'audit_events', ['actor_id'])
    op.create_index('ix_audit_events_action', 'audit_events', ['action'])
    op.create_index('ix_audit_events_allowed', 'audit_events', ['allowed'])


def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('task_artifacts')
    op.drop_table('execution_runs')

    op.drop_index('ix_verification_results_step_id', table_name='verification_results')
    op.drop_index('ix_tool_usage_step_id', table_name='tool_usage')
    op.drop_index('ix_recovery_attempts_step_id', table_name='recovery_attempts')

    op.drop_index('ix_task_events_execution_run_id', table_name='task_events')
    op.drop_column('task_events', 'execution_run_id')

    op.drop_index('ix_tasks_outcome', table_name='tasks')
    op.drop_column('tasks', 'outcome')

"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-09-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users table
    op.create_table(
        'users',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('email', sa.String(255), unique=True, nullable=False),
        sa.Column('hashed_password', sa.String(255), nullable=False),
        sa.Column('full_name', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True, nullable=False),
        sa.Column('is_verified', sa.Boolean(), default=False, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # Projects table
    op.create_table(
        'projects',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(50), default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE', name='fk_projects_user_id'),
    )
    op.create_index('ix_projects_user_id', 'projects', ['user_id'])

    # Tasks table
    op.create_table(
        'tasks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('project_id', sa.String(36), nullable=True),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('goal', sa.Text(), nullable=True),
        sa.Column('status', sa.String(50), default='created', nullable=False),
        sa.Column('mode', sa.String(50), default='balanced', nullable=False),
        sa.Column('risk_level', sa.String(20), default='low', nullable=False),
        sa.Column('definition_of_done', sa.JSON(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE', name='fk_tasks_user_id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL', name='fk_tasks_project_id'),
    )
    op.create_index('ix_tasks_user_id', 'tasks', ['user_id'])
    op.create_index('ix_tasks_project_id', 'tasks', ['project_id'])
    op.create_index('ix_tasks_status', 'tasks', ['status'])

    # Task steps table
    op.create_table(
        'task_steps',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('parent_id', sa.String(36), nullable=True),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(50), default='pending', nullable=False),
        sa.Column('order', sa.Integer(), nullable=False, default=0),
        sa.Column('dependencies', sa.String(500), nullable=True),
        sa.Column('model_used', sa.String(255), nullable=True),
        sa.Column('tools_used', sa.String(500), nullable=True),
        sa.Column('timeout', sa.Integer(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE', name='fk_task_steps_task_id'),
    )
    op.create_index('ix_task_steps_task_id', 'task_steps', ['task_id'])

    # Task events table
    op.create_table(
        'task_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('step_id', sa.String(36), nullable=True),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE', name='fk_task_events_task_id'),
        sa.ForeignKeyConstraint(['step_id'], ['task_steps.id'], ondelete='CASCADE', name='fk_task_events_step_id'),
    )
    op.create_index('ix_task_events_task_id', 'task_events', ['task_id'])
    op.create_index('ix_task_events_step_id', 'task_events', ['step_id'])

    # Model metrics table
    op.create_table(
        'model_metrics',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('provider', sa.String(100), nullable=False),
        sa.Column('model_name', sa.String(255), nullable=False),
        sa.Column('task_type', sa.String(100), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('cost_usd', sa.Float(), nullable=True, default=0.0),
        sa.Column('tokens_input', sa.Integer(), nullable=True),
        sa.Column('tokens_output', sa.Integer(), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False, default=True),        sa.Column('task_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL', name='fk_model_metrics_task_id'),
    )
    op.create_index('ix_model_metrics_provider', 'model_metrics', ['provider'])
    op.create_index('ix_model_metrics_task_id', 'model_metrics', ['task_id'])

    # Tool usage table
    op.create_table(
        'tool_usage',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('step_id', sa.String(36), nullable=True),
        sa.Column('tool_id', sa.String(100), nullable=False),
        sa.Column('tool_name', sa.String(255), nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=True),
        sa.Column('result_summary', sa.JSON(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE', name='fk_tool_usage_task_id'),
    )
    op.create_index('ix_tool_usage_task_id', 'tool_usage', ['task_id'])

    # Verification results table
    op.create_table(
        'verification_results',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('step_id', sa.String(36), nullable=True),
        sa.Column('verified', sa.Boolean(), nullable=False, default=False),
        sa.Column('confidence', sa.Float(), nullable=False, default=0.0),
        sa.Column('evidence', sa.JSON(), nullable=False, default=[]),
        sa.Column('failed_checks', sa.JSON(), nullable=False, default=[]),
        sa.Column('verification_type', sa.String(100), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE', name='fk_verification_results_task_id'),
        sa.ForeignKeyConstraint(['step_id'], ['task_steps.id'], ondelete='CASCADE', name='fk_verification_results_step_id'),
    )
    op.create_index('ix_verification_results_task_id', 'verification_results', ['task_id'])

    # Recovery attempts table
    op.create_table(
        'recovery_attempts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('task_id', sa.String(36), nullable=False),
        sa.Column('step_id', sa.String(36), nullable=True),
        sa.Column('failure_category', sa.String(100), nullable=False),
        sa.Column('failure_description', sa.Text(), nullable=False),
        sa.Column('diagnosis', sa.Text(), nullable=True),
        sa.Column('recovery_strategy', sa.Text(), nullable=True),
        sa.Column('alternatives_considered', sa.JSON(), nullable=True, default=[]),
        sa.Column('success', sa.Boolean(), nullable=False, default=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE', name='fk_recovery_attempts_task_id'),
        sa.ForeignKeyConstraint(['step_id'], ['task_steps.id'], ondelete='CASCADE', name='fk_recovery_attempts_step_id'),
    )
    op.create_index('ix_recovery_attempts_task_id', 'recovery_attempts', ['task_id'])

    # Audit logs table
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('action', sa.String(100), nullable=False),
        sa.Column('resource_type', sa.String(100), nullable=False),
        sa.Column('resource_id', sa.String(36), nullable=False),
        sa.Column('permissions_required', sa.JSON(), nullable=True, default=[]),
        sa.Column('risk_level', sa.String(20), nullable=True, default='low'),
        sa.Column('success', sa.Boolean(), nullable=False, default=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE', name='fk_audit_logs_user_id'),
    )
    op.create_index('ix_audit_logs_user_id', 'audit_logs', ['user_id'])

    # Memory table
    op.create_table(
        'memory',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, default=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE', name='fk_memory_user_id'),
    )
    op.create_index('ix_memory_user_id', 'memory', ['user_id'])

    # Skills table
    op.create_table(
        'skills',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(255), unique=True, nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('version', sa.String(50), nullable=False, default='1.0.0'),
        sa.Column('required_tools', sa.JSON(), nullable=False, default=[]),
        sa.Column('required_models', sa.JSON(), nullable=False, default=[]),
        sa.Column('permissions', sa.JSON(), nullable=False, default=[]),
        sa.Column('input_schema', sa.JSON(), nullable=False, default={}),
        sa.Column('output_schema', sa.JSON(), nullable=False, default={}),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('category', sa.String(100), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True, default={}),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_skills_category', 'skills', ['category'])

    # NOTE: foreign keys are declared INLINE in each create_table above.
    # Post-hoc op.create_foreign_key() emits ALTER TABLE, which SQLite
    # cannot apply (no constraint ALTER support) — inline constraints are
    # plain CREATE TABLE on both SQLite and PostgreSQL.


def downgrade() -> None:
    # Drop tables (children before parents). Dropping a table removes its
    # inline foreign keys on both dialects, so no separate ALTER needed.
    op.drop_table('skills')
    op.drop_table('memory')
    op.drop_table('audit_logs')
    op.drop_table('recovery_attempts')
    op.drop_table('verification_results')
    op.drop_table('tool_usage')
    op.drop_table('model_metrics')
    op.drop_table('task_events')
    op.drop_table('task_steps')
    op.drop_table('tasks')
    op.drop_table('projects')
    op.drop_table('users')

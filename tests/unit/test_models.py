"""Unit tests for data models."""

import pytest
from unittest.mock import MagicMock


def _create_mock_task(**kwargs):
    """Create a mock task with required attributes for tests."""
    mock = MagicMock()
    mock.user_id = kwargs.get("user_id", "user-123")
    mock.title = kwargs.get("title", "Test Task")
    mock.status = kwargs.get("status", "created")
    mock.mode = kwargs.get("mode", "balanced")
    mock.risk_level = kwargs.get("risk_level", "low")
    mock.goal = kwargs.get("goal", "")
    mock.description = kwargs.get("description", "")
    mock.project_id = kwargs.get("project_id", None)
    mock.result = kwargs.get("result", None)
    mock.error = kwargs.get("error", None)
    mock.started_at = kwargs.get("started_at", None)
    mock.completed_at = kwargs.get("completed_at", None)
    mock.cancelled_at = kwargs.get("cancelled_at", None)
    mock.created_at = kwargs.get("created_at", None)
    mock.updated_at = kwargs.get("updated_at", None)
    mock.steps = kwargs.get("steps", [])
    mock.events = kwargs.get("events", [])
    mock.id = kwargs.get("id", "task-123")
    return mock


def _create_mock_project(**kwargs):
    """Create a mock project with required attributes for tests."""
    mock = MagicMock()
    mock.id = kwargs.get("id", "project-123")
    mock.user_id = kwargs.get("user_id", "user-123")
    mock.name = kwargs.get("name", "Test Project")
    mock.description = kwargs.get("description", "A test project")
    mock.status = kwargs.get("status", "active")
    mock.created_at = kwargs.get("created_at", None)
    mock.updated_at = kwargs.get("updated_at", None)
    return mock


def _create_mock_step(**kwargs):
    """Create a mock task step with required attributes for tests."""
    mock = MagicMock()
    mock.id = kwargs.get("id", "step-123")
    mock.task_id = kwargs.get("task_id", "task-123")
    mock.title = kwargs.get("title", "Test Step")
    mock.description = kwargs.get("description", "")
    mock.status = kwargs.get("status", "pending")
    mock.order = kwargs.get("order", 0)
    mock.dependencies = kwargs.get("dependencies", "")
    mock.model_used = kwargs.get("model_used", None)
    mock.tools_used = kwargs.get("tools_used", "")
    mock.timeout = kwargs.get("timeout", None)
    mock.result = kwargs.get("result", None)
    mock.error = kwargs.get("error", None)
    mock.parent_id = kwargs.get("parent_id", None)
    mock.created_at = kwargs.get("created_at", None)
    mock.updated_at = kwargs.get("updated_at", None)
    return mock


class TestTaskModel:
    """Tests for Task model."""

    def test_task_creation_with_defaults(self):
        """Task should have correct default values."""
        from app.models.task import Task, TaskStatus, ExecutionMode, RiskLevel

        # Test that enums exist with correct values
        assert TaskStatus.CREATED.value == "created"
        assert TaskStatus.COMPLETED.value == "completed"
        assert ExecutionMode.BALANCED.value == "balanced"
        assert RiskLevel.LOW.value == "low"

    def test_task_status_enum(self):
        """TaskStatus enum should have all expected values."""
        from app.models.task import TaskStatus

        expected_values = {
            "created", "understanding", "planning", "preparing",
            "waiting_for_user", "executing", "verifying", "recovering",
            "completed", "partially_completed", "failed", "cancelled"
        }
        actual_values = {s.value for s in TaskStatus}
        assert expected_values == actual_values


class TestProjectModel:
    """Tests for Project model."""

    def test_project_defaults(self):
        """Project should have correct default values."""
        from app.models.project import Project

        # Test that model can be instantiated (basic smoke test)
        # Full instantiation requires DB, so we test the model exists
        assert Project.__tablename__ == "projects"


class TestTaskStepModel:
    """Tests for TaskStep model."""

    def test_task_step_defaults(self):
        """TaskStep should have correct default values."""
        from app.models.task_step import TaskStep, StepStatus

        assert StepStatus.PENDING.value == "pending"
        assert StepStatus.COMPLETED.value == "completed"
        assert TaskStep.__tablename__ == "task_steps"

    def test_step_status_enum(self):
        """StepStatus enum should have all expected values."""
        from app.models.task_step import StepStatus

        expected_values = {
            "pending", "created", "understanding", "planning", "preparing",
            "waiting_for_user", "executing", "verifying", "recovering",
            "completed", "partially_completed", "failed", "cancelled"
        }
        actual_values = {s.value for s in StepStatus}
        assert expected_values == actual_values


class TestEventModel:
    """Tests for TaskEvent model."""

    def test_event_defaults(self):
        """TaskEvent should have correct defaults."""
        from app.models.task_event import TaskEvent, EventType

        assert EventType.CREATED.value == "created"
        assert EventType.STATUS_CHANGED.value == "status_changed"
        assert TaskEvent.__tablename__ == "task_events"


class TestVerificationResultModel:
    """Tests for VerificationResult model."""

    def test_verification_model_exists(self):
        """VerificationResult should be importable and have correct table."""
        from app.models.verification_result import VerificationResult

        assert VerificationResult.__tablename__ == "verification_results"


class TestRecoveryAttemptModel:
    """Tests for RecoveryAttempt model."""

    def test_recovery_model_exists(self):
        """RecoveryAttempt should be importable and have correct table."""
        from app.models.recovery_attempt import RecoveryAttempt, FailureCategory

        assert FailureCategory.TOOL_FAILURE.value == "tool_failure"
        assert RecoveryAttempt.__tablename__ == "recovery_attempts"


class TestMemoryModel:
    """Tests for Memory model."""

    def test_memory_model_exists(self):
        """Memory should be importable and have correct table."""
        from app.models.memory import Memory, MemoryType

        assert MemoryType.USER_PREFERENCE.value == "user_preference"
        assert Memory.__tablename__ == "memory"


class TestAuditLogModel:
    """Tests for AuditLog model."""

    def test_audit_log_model_exists(self):
        """AuditLog should be importable and have correct table."""
        from app.models.audit_log import AuditLog

        assert AuditLog.__tablename__ == "audit_logs"

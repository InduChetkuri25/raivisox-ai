"""Unit tests for authentication."""

import pytest
from datetime import datetime


class TestPasswordHashing:
    """Tests for password hashing functionality."""

    @pytest.mark.skip(reason="bcrypt version incompatibility in test environment")
    def test_password_hash_is_not_plain_text(self):
        """Password hash should not be the same as the password."""
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

        password = "test123"
        hashed = pwd_context.hash(password)

        assert hashed != password
        assert hashed.startswith("$2")

    @pytest.mark.skip(reason="bcrypt version incompatibility in test environment")
    def test_password_verification_works(self):
        """Correct password should verify, incorrect should not."""
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

        password = "test123"
        hashed = pwd_context.hash(password)

        assert pwd_context.verify(password, hashed)
        assert not pwd_context.verify("wrong", hashed)


class TestJWTTokens:
    """Tests for JWT token functionality."""

    def test_token_creation_and_decode(self):
        """Token should be created and decoded correctly."""
        from jose import jwt
        from datetime import timedelta

        secret = "test-secret-key"
        algorithm = "HS256"

        payload = {
            "sub": "user-123",
            "email": "test@example.com",
            "exp": datetime.utcnow() + timedelta(minutes=60),
            "iat": datetime.utcnow(),
            "type": "access"
        }

        token = jwt.encode(payload, secret, algorithm=algorithm)
        decoded = jwt.decode(token, secret, algorithms=[algorithm])

        assert decoded["sub"] == "user-123"
        assert decoded["email"] == "test@example.com"

    def test_invalid_token_fails_decode(self):
        """Invalid tokens should fail to decode."""
        from jose import JWTError
        from jose import jwt

        secret = "test-secret-key"
        algorithm = "HS256"

        with pytest.raises(JWTError):
            jwt.decode("invalid-token", secret, algorithms=[algorithm])


class TestTaskStatusSchema:
    """Tests for task status enumeration in schemas."""

    def test_task_status_values(self):
        """All expected status values should exist in schema."""
        from app.models.task import TaskStatus

        expected_values = {
            "created", "understanding", "planning", "preparing",
            "waiting_for_user", "executing", "verifying", "recovering",
            "completed", "partially_completed", "failed", "cancelled"
        }
        actual_values = {s.value for s in TaskStatus}
        assert expected_values == actual_values

    def test_task_status_is_enum(self):
        """TaskStatus should be usable as an enum."""
        from app.models.task import TaskStatus

        assert TaskStatus.CREATED.value == "created"
        assert TaskStatus.COMPLETED.value == "completed"


class TestExecutionModeSchema:
    """Tests for execution mode enumeration in schemas."""

    def test_execution_mode_values(self):
        """All execution modes should be defined in schema."""
        from app.models.task import ExecutionMode

        assert ExecutionMode.FAST.value == "fast"
        assert ExecutionMode.BALANCED.value == "balanced"
        assert ExecutionMode.QUALITY.value == "quality"
        assert ExecutionMode.ECONOMY.value == "economy"


class TestRiskLevelSchema:
    """Tests for risk level enumeration in schemas."""

    def test_risk_level_values(self):
        """All risk levels should be defined in schema."""
        from app.models.task import RiskLevel

        assert RiskLevel.LOW.value == "low"
        assert RiskLevel.MEDIUM.value == "medium"
        assert RiskLevel.HIGH.value == "high"
        assert RiskLevel.CRITICAL.value == "critical"


class TestSchemas:
    """Tests for Pydantic schemas."""

    def test_task_create_schema(self):
        """TaskCreate schema should validate correctly."""
        from app.tasks.schemas import TaskCreate

        # Valid data
        task = TaskCreate(title="Test Task")
        assert task.title == "Test Task"
        assert task.description is None

    def test_task_create_with_all_fields(self):
        """TaskCreate schema should accept all optional fields."""
        from app.tasks.schemas import TaskCreate

        task = TaskCreate(
            title="Test",
            description="Description",
            goal="Goal",
            mode="fast",
        )
        assert task.title == "Test"
        assert task.description == "Description"
        assert task.goal == "Goal"
        assert task.mode == "fast"

    def test_task_update_schema(self):
        """TaskUpdate schema should work with partial updates."""
        from app.tasks.schemas import TaskUpdate

        update = TaskUpdate(title="New Title")
        assert update.title == "New Title"
        assert update.description is None

    def test_task_step_create_schema(self):
        """TaskStepCreate schema should validate correctly."""
        from app.tasks.schemas import TaskStepCreate

        step = TaskStepCreate(title="Step 1", order=0)
        assert step.title == "Step 1"
        assert step.order == 0

    def test_project_create_schema(self):
        """ProjectCreate schema should validate correctly."""
        from app.tasks.schemas import ProjectCreate

        project = ProjectCreate(name="Test Project")
        assert project.name == "Test Project"
        assert project.description is None

"""Shared test fixtures for unit tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_async_session():
    """Create a mock async database session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.refresh = MagicMock()
    return session


@pytest.fixture
def mock_user():
    """Create a mock user object."""
    from unittest.mock import MagicMock

    user = MagicMock()
    user.id = "test-user-id"
    user.email = "test@example.com"
    user.hashed_password = "hashed_password"
    user.full_name = "Test User"
    user.is_active = True
    user.is_verified = False
    user.created_at = None
    user.updated_at = None
    return user

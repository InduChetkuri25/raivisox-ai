"""Authentication schemas."""

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# Boundary validation for emails: strict enough to reject header injection
# (whitespace/control characters), absurd lengths and missing/multiple
# '@' — while still accepting reserved test domains like '.test' that full
# deliverability validators reject. The database unique constraint remains
# the authority on duplicates.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    if not v or len(v) > 254 or not _EMAIL_RE.match(v):
        raise ValueError("invalid email address")
    return v


class UserCreate(BaseModel):
    """Schema for user registration."""

    email: str
    password: str = Field(..., min_length=8, max_length=128)
    full_name: Optional[str] = Field(None, max_length=255)

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        validated = _validate_email(v)
        if validated is None:
            raise ValueError("email is required")
        return validated


class UserLogin(BaseModel):
    """Schema for user login."""

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        validated = _validate_email(v)
        if validated is None:
            raise ValueError("email is required")
        return validated


class Token(BaseModel):
    """Schema for JWT token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    """Schema for token payload data."""

    user_id: Optional[str] = None
    email: Optional[str] = None


class UserResponse(BaseModel):
    """Schema for user response."""

    id: str
    email: str
    full_name: Optional[str] = None
    is_active: bool
    is_verified: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    """Schema for user update."""

    full_name: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: Optional[str]) -> Optional[str]:
        return _validate_email(v)

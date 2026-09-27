"""Configuration settings for RAIVISOX API.

Environment model:
- development (default): convenient defaults, the known development
  SECRET_KEY is allowed (a loud warning is printed at startup).
- production: `ENVIRONMENT=production` FAILS FAST when required
  configuration is missing or still a development default. Error
  messages name the environment variable — never the value, so no
  secret can leak through a startup error.
"""

import json
import os
from functools import lru_cache
from typing import Annotated, List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Known development defaults — refused in production.
DEV_SECRET_KEY = "dev-secret-key-change-in-production-12345"
DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/raivisox"

# Substrings that mark a SECRET_KEY as a placeholder, checked
# case-insensitively. Only variable NAMES are ever echoed in errors.
_SECRET_PLACEHOLDER_MARKERS = (
    "dev-secret-key",
    "change-this",
    "change-me",
    "changeme",
    "your-secret-key",
    "example",
    "password",
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "RAIVISOX AI API"
    app_version: str = "0.1.0"
    debug: bool = False
    # "development" | "test" | "production"
    environment: str = "development"

    # Database
    database_url: str = DEV_DATABASE_URL
    database_url_async: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Security
    secret_key: str = DEV_SECRET_KEY
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 24 hours

    # CORS
    allowed_hosts: Annotated[List[str], NoDecode] = []
    cors_origins: Annotated[List[str], NoDecode] = []

    # AI Providers (optional in development; required in production)
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None

    # Execution
    default_execution_mode: str = "BALANCED"
    max_execution_budget: Optional[float] = None
    default_task_timeout: int = 300

    # Artifact storage limits (bytes / counts). See docs/deployment.md.
    max_artifact_size_bytes: int = 50 * 1024 * 1024        # 50 MiB per artifact
    max_artifacts_per_task: int = 1000                     # registry rows per task
    max_zip_size_bytes: int = 200 * 1024 * 1024            # 200 MiB per bundle
    max_zip_files: int = 10000                             # entries per bundle
    # 0 = keep artifacts forever (default; history semantics untouched).
    artifact_retention_days: int = 0
    # Interval between scheduled retention passes, in seconds. Only used
    # when artifact_retention_days > 0 (the worker is not started at 0).
    artifact_cleanup_interval_seconds: int = 6 * 3600

    # Logging
    log_level: str = "INFO"

    @field_validator("allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def _split_origin_list(cls, v):
        """Accept comma-separated strings, JSON arrays, or real lists.

        pydantic-settings would otherwise demand strict JSON for list
        fields — which made the documented `CORS_ORIGINS=a,b` form crash
        the API at import time.
        """
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return list(v)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                parsed = json.loads(s)
                if not isinstance(parsed, list):
                    raise ValueError("expected a JSON array of origins")
                return [str(x) for x in parsed]
            return [p.strip() for p in s.split(",") if p.strip()]
        return v

    def model_post_init(self, __context):
        """Post-init processing."""
        if not self.database_url_async:
            # Convert sync URL to async if needed
            if self.database_url.startswith("postgresql://"):
                self.database_url_async = self.database_url.replace(
                    "postgresql://", "postgresql+asyncpg://"
                )
            elif self.database_url.startswith("postgres://"):
                self.database_url_async = self.database_url.replace(
                    "postgres://", "postgresql+asyncpg://"
                )
            else:
                self.database_url_async = self.database_url


def provider_configured(s: "Settings") -> bool:
    """True when an LLM provider (Ollama endpoint or hosted API) is configured.

    Checked via settings fields AND the raw environment, because the
    adapters read RAIVISOX_LLM_* directly from os.environ.
    """
    if any((s.openai_api_key, s.anthropic_api_key, s.google_api_key)):
        return True
    for name in (
        "RAIVISOX_LLM_BASE_URL",
        "RAIVISOX_LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
    ):
        if os.environ.get(name, "").strip():
            return True
    return False


def _secret_is_placeholder(secret: str) -> bool:
    """True when the secret is empty, too short, or a known placeholder."""
    if not secret or len(secret) < 32:
        return True
    low = secret.lower()
    return any(marker in low for marker in _SECRET_PLACEHOLDER_MARKERS)


def validate_settings(s: "Settings") -> None:
    """Fail fast on unsafe/missing production configuration.

    Raises RuntimeError listing the offending VARIABLE NAMES only —
    values (secrets, URLs with credentials) are never echoed.

    Development and test environments always pass: local convenience is
    deliberately preserved.
    """
    env = (s.environment or "development").strip().lower()
    if env not in ("development", "test", "production"):
        raise RuntimeError(
            f"ENVIRONMENT must be one of development/test/production, got '{env}'."
        )
    if env != "production":
        return

    problems: List[str] = []

    if _secret_is_placeholder(s.secret_key):
        problems.append(
            "SECRET_KEY is missing, too short (<32 chars), or a known "
            "development/placeholder value (generate with `openssl rand -hex 32`)"
        )

    if not s.database_url or s.database_url == DEV_DATABASE_URL:
        problems.append(
            "DATABASE_URL is missing or still the development default; "
            "set an explicit production connection string"
        )
    elif not s.database_url.startswith(
        ("postgresql://", "postgresql+asyncpg://", "postgres://", "sqlite")
    ):
        problems.append(
            "DATABASE_URL must use a postgresql(+asyncpg) or sqlite driver"
        )

    if not s.cors_origins:
        problems.append(
            "CORS_ORIGINS must explicitly list the frontend origin(s) "
            "(e.g. https://app.example.com)"
        )

    if not s.allowed_hosts:
        problems.append(
            "ALLOWED_HOSTS must explicitly list the public hostname(s) "
            "(e.g. api.example.com)"
        )

    if s.debug:
        problems.append(
            "DEBUG must be false in production (debug mode returns internal "
            "error details to clients)"
        )

    if not provider_configured(s):
        problems.append(
            "no LLM provider configured: set RAIVISOX_LLM_BASE_URL "
            "(Ollama/LM Studio endpoint) or a provider API key "
            "(OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY / "
            "RAIVISOX_LLM_API_KEY)"
        )

    for name, value in (
        ("MAX_ARTIFACT_SIZE_BYTES", s.max_artifact_size_bytes),
        ("MAX_ARTIFACTS_PER_TASK", s.max_artifacts_per_task),
        ("MAX_ZIP_SIZE_BYTES", s.max_zip_size_bytes),
        ("MAX_ZIP_FILES", s.max_zip_files),
    ):
        if value <= 0:
            problems.append(f"{name} must be a positive integer")

    if s.artifact_retention_days < 0:
        problems.append(
            "ARTIFACT_RETENTION_DAYS must be 0 (keep forever) or a positive "
            "number of days"
        )
    if s.artifact_retention_days > 0 and s.artifact_cleanup_interval_seconds <= 0:
        problems.append(
            "ARTIFACT_CLEANUP_INTERVAL_SECONDS must be a positive number of "
            "seconds when ARTIFACT_RETENTION_DAYS > 0"
        )

    if problems:
        raise RuntimeError(
            "Refusing to start with ENVIRONMENT=production — invalid "
            "configuration:\n- " + "\n- ".join(problems)
        )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance (validated before first use)."""
    s = Settings()
    validate_settings(s)
    return s


settings = get_settings()

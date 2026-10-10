# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Environment-driven application settings.

Values come from process environment variables first (this is how Docker
injects them) and fall back to the repository-root ``.env`` file for local
development on the host. Invalid or missing configuration fails at startup.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> repository root. Missing files are ignored,
# so inside the container (where this path does not exist) only real
# environment variables are used.
_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

ASYNC_DATABASE_SCHEME = "postgresql+asyncpg"

AppEnv = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Values copied unchanged from .env.example are configuration mistakes.
_PLACEHOLDER_PREFIXES = ("replace-with", "https://replace-with")
_BASE_URL = re.compile(r"^https?://[^/\s]+(/\S*)?$")

# A browser Origin: scheme://host[:port], with no path and no trailing slash.
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+$")


def _split_comma_separated(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# NoDecode: read "a,b" as a plain string (not JSON) and split it ourselves.
CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_comma_separated)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must never echo raw values: DATABASE_URL contains a
        # password, and startup errors end up in container and CI logs.
        hide_input_in_errors=True,
    )

    app_env: AppEnv = "development"
    log_level: LogLevel = "INFO"

    # Contains a password, so it is a secret: masked in repr() and logs.
    database_url: SecretStr
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    db_health_timeout_seconds: float = Field(default=2.0, gt=0)

    # Browser origins allowed to read API responses (CORS). Empty by default:
    # no cross-origin access unless explicitly configured.
    cors_allowed_origins: CommaSeparated = []

    # Nebius Token Factory (OpenAI-compatible). Optional so the API can start
    # without AI configured; building a provider fails fast if they are missing.
    nebius_api_key: SecretStr | None = None
    nebius_base_url: str | None = None
    # Model roles, not model names, are what code asks for. IDs must be
    # verified against the live catalog (docs/experiments/model-catalog.md).
    model_fast: str | None = None
    model_strong: str | None = None
    embedding_model: str | None = None

    # Reliability bounds for every model call (see ADR-005).
    ai_request_timeout_seconds: float = Field(default=60.0, gt=0)
    ai_max_attempts: int = Field(default=3, ge=1, le=10)
    ai_backoff_base_seconds: float = Field(default=1.0, gt=0)
    ai_backoff_max_seconds: float = Field(default=20.0, gt=0)
    ai_max_repair_attempts: int = Field(default=1, ge=0, le=3)
    # Reasoning shares this budget, so it is far above the visible answer size.
    ai_max_output_tokens: int = Field(default=4096, ge=256)

    # Canonical store for original source bytes. No default: a relative or
    # implicit location would silently give each process (host CLI, API
    # container) its own store behind the same sha256: references.
    raw_storage_dir: Path | None = None

    # Safety limits for document ingestion. Inputs beyond them are rejected
    # as `limit_exceeded`: size and ZIP limits before parsing, PDF pages once
    # the document is open, extracted characters after parsing.
    ingest_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    ingest_max_pdf_pages: int = Field(default=500, ge=1)
    ingest_max_extracted_chars: int = Field(default=2_000_000, ge=1)
    ingest_max_zip_members: int = Field(default=2_000, ge=1)
    ingest_max_zip_uncompressed_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    ingest_max_zip_compression_ratio: float = Field(default=100.0, gt=1)

    @field_validator("raw_storage_dir", mode="before")
    @classmethod
    def _blank_storage_dir_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("raw_storage_dir")
    @classmethod
    def _require_absolute_storage_dir(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("RAW_STORAGE_DIR must be an absolute path")
        return value

    @field_validator(
        "nebius_api_key",
        "nebius_base_url",
        "model_fast",
        "model_strong",
        "embedding_model",
        mode="before",
    )
    @classmethod
    def _blank_is_unset_placeholder_is_error(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None  # `KEY=` in .env means "not configured"
            if value.startswith(_PLACEHOLDER_PREFIXES):
                raise ValueError("still a placeholder value; set a verified value or leave empty")
        return value

    @field_validator("nebius_base_url")
    @classmethod
    def _require_http_url(cls, value: str | None) -> str | None:
        # A malformed URL would otherwise surface as a retried "connection" error.
        if value is not None and not _BASE_URL.match(value):
            raise ValueError("NEBIUS_BASE_URL must be an http(s):// URL with a host")
        return value

    @model_validator(mode="after")
    def _backoff_bounds_ordered(self) -> Self:
        if self.ai_backoff_max_seconds < self.ai_backoff_base_seconds:
            raise ValueError("AI_BACKOFF_MAX_SECONDS must be >= AI_BACKOFF_BASE_SECONDS")
        return self

    @field_validator("cors_allowed_origins")
    @classmethod
    def _require_explicit_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if origin == "*":
                raise ValueError("CORS_ALLOWED_ORIGINS must list explicit origins, not '*'")
            if not _ORIGIN.match(origin):
                raise ValueError(
                    f"Invalid origin {origin!r}: expected scheme://host[:port] "
                    "with no path or trailing slash"
                )
        return origins

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(f"{ASYNC_DATABASE_SCHEME}://"):
            raise ValueError(f"DATABASE_URL must use the {ASYNC_DATABASE_SCHEME}:// scheme")
        return value


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once at startup."""
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment

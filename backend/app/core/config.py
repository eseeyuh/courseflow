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
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> repository root. Missing files are ignored,
# so inside the container (where this path does not exist) only real
# environment variables are used.
_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

ASYNC_DATABASE_SCHEME = "postgresql+asyncpg"

AppEnv = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

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

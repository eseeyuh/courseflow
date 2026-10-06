# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Environment-driven application settings.

Values come from process environment variables first (this is how Docker
injects them) and fall back to the repository-root ``.env`` file for local
development on the host. Invalid configuration fails at startup.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> repository root. Missing files are ignored,
# so inside the container (where this path does not exist) only real
# environment variables are used.
_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

AppEnv = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: AppEnv = "development"
    log_level: LogLevel = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once. Used as a FastAPI dependency."""
    return Settings()

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import configure_logging
from app.main import create_app

VALID_URL = "postgresql+asyncpg://courseflow:s3cret-pw@127.0.0.1:5432/courseflow"


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DATABASE_URL", VALID_URL)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_env == "production"
    assert settings.log_level == "WARNING"
    assert settings.database_url.get_secret_value() == VALID_URL


def test_invalid_setting_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_missing_database_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_non_async_database_driver_rejected() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+psycopg://courseflow:pw@127.0.0.1/courseflow",  # type: ignore[arg-type]
        )


def test_invalid_database_url_error_does_not_leak_password() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql://courseflow:s3cret-pw@db/courseflow",  # type: ignore[arg-type]
        )

    assert "database_url" in str(excinfo.value)
    assert "s3cret-pw" not in str(excinfo.value)


@pytest.mark.parametrize(
    "field", ["NEBIUS_API_KEY", "NEBIUS_BASE_URL", "MODEL_FAST", "MODEL_STRONG", "EMBEDDING_MODEL"]
)
def test_blank_ai_setting_means_not_configured(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv(field, "  ")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert getattr(settings, field.lower()) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("NEBIUS_API_KEY", "replace-with-your-key"),
        ("NEBIUS_BASE_URL", "https://replace-with-token-factory-base-url/v1"),
        ("MODEL_FAST", "replace-with-verified-nemotron-model-id"),
        ("MODEL_STRONG", "replace-with-verified-nemotron-model-id"),
        ("EMBEDDING_MODEL", "replace-with-verified-embedding-model-id"),
    ],
)
def test_example_placeholders_fail_fast(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv(field, value)

    with pytest.raises(ValidationError, match=field.lower()):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_env_example_parses_with_ai_unconfigured() -> None:
    """A fresh `cp .env.example .env` must start: no placeholder may fail fast."""
    example = Path(__file__).resolve().parents[2] / ".env.example"

    settings = Settings(_env_file=example)  # type: ignore[call-arg]

    assert settings.nebius_api_key is None
    assert settings.model_fast and settings.model_strong and settings.nebius_base_url


def test_ai_settings_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("MODEL_FAST", "  vendor/model  ")

    assert Settings(_env_file=None).model_fast == "vendor/model"  # type: ignore[call-arg]


@pytest.mark.parametrize("url", ["api.example.com/v1", "ftp://example.com/v1", "https://"])
def test_malformed_base_url_fails_fast(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("NEBIUS_BASE_URL", url)

    with pytest.raises(ValidationError, match="nebius_base_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AI_MAX_ATTEMPTS", "0"),
        ("AI_MAX_ATTEMPTS", "11"),
        ("AI_MAX_REPAIR_ATTEMPTS", "4"),
        ("AI_MAX_OUTPUT_TOKENS", "100"),
        ("AI_REQUEST_TIMEOUT_SECONDS", "0"),
    ],
)
def test_ai_reliability_bounds_are_enforced(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv(field, value)

    with pytest.raises(ValidationError, match=field.lower()):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_backoff_max_below_base_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("AI_BACKOFF_BASE_SECONDS", "5")
    monkeypatch.setenv("AI_BACKOFF_MAX_SECONDS", "2")

    with pytest.raises(ValidationError, match="AI_BACKOFF_MAX_SECONDS"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_nebius_api_key_is_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-very-secret-key")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.nebius_api_key is not None
    assert settings.nebius_api_key.get_secret_value() == "nb-very-secret-key"
    assert "nb-very-secret-key" not in repr(settings)
    assert "nb-very-secret-key" not in str(settings.model_dump())


def test_database_url_is_masked_in_repr() -> None:
    settings = Settings(_env_file=None, database_url=VALID_URL)  # type: ignore[call-arg, arg-type]

    assert "s3cret-pw" not in repr(settings)
    assert "s3cret-pw" not in str(settings.model_dump())


@pytest.mark.anyio
async def test_startup_log_masks_database_password(capsys: pytest.CaptureFixture[str]) -> None:
    app = create_app(Settings(_env_file=None, database_url=VALID_URL))  # type: ignore[call-arg, arg-type]

    async with app.router.lifespan_context(app):  # engine creation opens no connection
        pass

    out = capsys.readouterr().out
    assert '"event": "app.startup"' in out
    assert "courseflow:***@127.0.0.1" in out
    assert "s3cret-pw" not in out


def test_logs_are_json_lines_with_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    logging.getLogger("courseflow.test").info("app.startup", extra={"app_env": "test"})

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "app.startup"
    assert record["level"] == "INFO"
    assert record["app_env"] == "test"
    assert "ts" in record

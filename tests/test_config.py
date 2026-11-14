from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings, secret_value


def test_settings_env_file_is_project_absolute_path() -> None:
    env_file = Settings.model_config["env_file"]

    assert isinstance(env_file, Path)
    assert env_file.is_absolute()
    assert env_file.name == ".env"
    assert env_file.parent == Path(__file__).resolve().parents[1]


def test_settings_expose_optional_fallback_fields() -> None:
    settings = Settings(_env_file=None)

    assert settings.openai_stream_reasoning_effort == "medium"
    assert settings.openai_fallback_model == ""
    assert settings.openai_fallback_reasoning_effort == ""


def test_settings_expose_worker_loop_fields() -> None:
    settings = Settings(_env_file=None)

    assert settings.worker_batch_limit == 10
    assert settings.worker_poll_interval_seconds == 5.0
    assert settings.worker_max_cycles is None


def test_settings_expose_embedding_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_timeout_seconds == 30.0
    assert settings.embedding_fallback_mode == "keyword"
    assert settings.embedding_base_url == ""
    assert secret_value(settings.embedding_api_key) == ""


def test_settings_hide_credentials_in_repr() -> None:
    placeholders = {
        "openai_api_key": "YOUR_API_KEY_HERE",
        "embedding_api_key": "YOUR_EMBEDDING_API_KEY_HERE",
        "fanqie_password": "YOUR_PASSWORD_HERE",
    }
    settings = Settings(_env_file=None, **placeholders)

    for name, placeholder in placeholders.items():
        value = getattr(settings, name)
        assert isinstance(value, SecretStr)
        assert placeholder not in repr(settings)
        assert secret_value(value) == placeholder


@pytest.mark.parametrize(
    "overrides",
    [
        {"embedding_model": "   "},
        {"embedding_timeout_seconds": 0},
        {"embedding_timeout_seconds": -1},
    ],
)
def test_settings_reject_invalid_embedding_configuration(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)

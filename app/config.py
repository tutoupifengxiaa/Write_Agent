from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite+pysqlite:///:memory:"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com"
    openai_model: str = "gpt-5.5"
    openai_review_model: str = "gpt-5.5"
    openai_reasoning_effort: str = "xhigh"
    openai_stream_reasoning_effort: str = "medium"
    openai_fallback_model: str = ""
    openai_fallback_reasoning_effort: str = ""
    openai_store_response: bool = False
    openai_timeout_seconds: float = 120.0
    embedding_model: str = "text-embedding-3-small"
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_fallback_mode: Literal["keyword", "disabled"] = "keyword"
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    fanqie_browser_mode: str = "stub"
    fanqie_author_url: str = ""
    fanqie_username: str = ""
    fanqie_password: str = ""
    fanqie_screenshot_dir: str = "logs"
    fanqie_headless: bool = True
    fanqie_slow_mo_ms: int = 0
    publish_mode: str = ""
    worker_batch_limit: int = 10
    worker_poll_interval_seconds: float = 5.0
    worker_max_cycles: int | None = None

    @field_validator("embedding_model")
    @classmethod
    def validate_embedding_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("embedding model is required")
        return normalized

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()

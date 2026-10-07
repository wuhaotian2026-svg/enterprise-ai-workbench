from __future__ import annotations

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProbeSettings(BaseSettings):
    """Minimal provider configuration used by the protocol probe only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        repr=False,
    )

    model_base_url: AnyHttpUrl
    model_api_key: SecretStr
    chat_model: str = Field(min_length=1)
    model_timeout_seconds: float = Field(default=30, gt=0, le=300)

    @field_validator("chat_model")
    @classmethod
    def reject_blank_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

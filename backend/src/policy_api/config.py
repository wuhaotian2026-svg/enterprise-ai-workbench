from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        repr=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str
    session_secret: SecretStr = Field(min_length=32)
    frontend_origins: str = "http://localhost:5173"
    trusted_hosts: str = "localhost,127.0.0.1,testserver,test"
    upload_root: Path = Path("./uploads")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

    model_base_url: AnyHttpUrl
    model_api_key: SecretStr
    chat_model: str = Field(min_length=1)
    model_disable_thinking: bool = False
    embedding_base_url: AnyHttpUrl | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str = Field(min_length=1)
    embedding_dimension: int = Field(default=1536, ge=1, le=65536)
    embedding_query_prefix: str = ""
    embedding_passage_prefix: str = ""
    model_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    retrieval_top_k: int = Field(default=8, ge=1, le=50)
    evidence_threshold: float = Field(default=0.72, ge=0, le=1)
    retrieval_lexical_candidate_limit: int = Field(default=20, ge=1, le=100)
    retrieval_vector_candidate_limit: int = Field(default=20, ge=1, le=100)
    retrieval_fused_top_k: int = Field(default=12, ge=1, le=50)
    evidence_max_chunks: int = Field(default=6, ge=1, le=12)
    evidence_lexical_threshold: float = Field(default=0.28, ge=0, le=1)
    evidence_semantic_threshold: float = Field(default=0.78, ge=0, le=1)
    evidence_dual_channel_minimum: float = Field(default=0.20, ge=0, le=1)
    query_variant_max_count: int = Field(default=3, ge=1, le=3)
    retrieval_allow_vector_only_fallback: bool = False
    login_rate_limit_per_minute: int = Field(default=10, ge=1, le=10000)
    question_rate_limit_per_minute: int = Field(default=20, ge=1, le=10000)
    tool_max_model_calls: int = Field(default=3, ge=1, le=10)
    tool_max_read_calls: int = Field(default=4, ge=1, le=20)
    tool_confirmation_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    hr_turn_rate_limit_per_minute: int = Field(default=20, ge=1, le=10000)
    hr_confirmation_rate_limit_per_minute: int = Field(
        default=10, ge=1, le=10000
    )

    @field_validator("database_url", "chat_model", "embedding_model")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @cached_property
    def frontend_origin_list(self) -> list[str]:
        return [item.strip() for item in self.frontend_origins.split(",") if item.strip()]

    @cached_property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def resolved_embedding_base_url(self) -> str:
        return str(self.embedding_base_url or self.model_base_url)

    @property
    def resolved_embedding_api_key(self) -> str:
        secret = self.embedding_api_key or self.model_api_key
        return secret.get_secret_value()

    def __repr__(self) -> str:
        return (
            "Settings("
            f"app_env={self.app_env!r}, "
            f"database_url={'***' if self.database_url else ''!r}, "
            f"frontend_origins={self.frontend_origins!r}, "
            f"upload_root={str(self.upload_root)!r}, "
            f"model_base_url={str(self.model_base_url)!r}, "
            f"embedding_base_url={self.resolved_embedding_base_url!r}, "
            f"chat_model={self.chat_model!r}, "
            f"model_disable_thinking={self.model_disable_thinking!r}, "
            f"embedding_model={self.embedding_model!r}"
            ")"
        )

from __future__ import annotations

import pytest
from pydantic import ValidationError

from policy_api.config import Settings


def valid_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "postgresql+psycopg://policy:secret@db/policy_test",
        "session_secret": "s" * 32,
        "frontend_origins": "http://localhost:5173",
        "upload_root": "./uploads-test",
        "model_base_url": "https://models.example.test/v1",
        "model_api_key": "api-key-that-must-stay-secret",
        "chat_model": "chat-test",
        "embedding_model": "embedding-test",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "missing_field",
    [
        "database_url",
        "session_secret",
        "model_base_url",
        "model_api_key",
        "chat_model",
        "embedding_model",
    ],
)
def test_required_production_settings_fail_when_missing(missing_field: str) -> None:
    values = valid_settings().model_dump()
    values["app_env"] = "production"
    values.pop(missing_field)

    with pytest.raises(ValidationError):
        Settings(**values)


def test_session_secret_must_be_at_least_32_characters() -> None:
    with pytest.raises(ValidationError):
        valid_settings(session_secret="too-short")


def test_secret_values_are_not_exposed_in_repr() -> None:
    settings = valid_settings()

    rendered = repr(settings)

    assert "api-key-that-must-stay-secret" not in rendered
    assert "s" * 32 not in rendered


def test_frontend_origins_are_normalized_to_a_list() -> None:
    settings = valid_settings(
        frontend_origins="http://localhost:5173, https://policy.example.test"
    )

    assert settings.frontend_origin_list == [
        "http://localhost:5173",
        "https://policy.example.test",
    ]


def test_embedding_runtime_can_use_a_separate_local_endpoint() -> None:
    settings = valid_settings(
        embedding_base_url="http://embeddings:8001/v1",
        embedding_api_key="local-only",
        embedding_query_prefix="query: ",
        embedding_passage_prefix="passage: ",
    )

    assert str(settings.embedding_base_url).rstrip("/") == "http://embeddings:8001/v1"
    assert settings.embedding_api_key.get_secret_value() == "local-only"
    assert settings.embedding_query_prefix == "query: "
    assert settings.embedding_passage_prefix == "passage: "


def test_embedding_runtime_defaults_to_the_chat_provider() -> None:
    settings = valid_settings()

    assert settings.resolved_embedding_base_url == str(settings.model_base_url)
    assert settings.resolved_embedding_api_key == settings.model_api_key.get_secret_value()


def test_provider_specific_thinking_capability_is_explicit_and_defaults_off() -> None:
    assert valid_settings().model_disable_thinking is False
    assert valid_settings(model_disable_thinking=True).model_disable_thinking is True


def test_rag_retrieval_settings_are_bounded_and_do_not_reuse_legacy_threshold() -> None:
    settings = valid_settings(evidence_threshold=0.015)

    assert settings.retrieval_lexical_candidate_limit == 20
    assert settings.retrieval_vector_candidate_limit == 20
    assert settings.retrieval_fused_top_k == 12
    assert settings.evidence_max_chunks == 6
    assert settings.evidence_lexical_threshold == 0.28
    assert settings.evidence_semantic_threshold == 0.78
    assert settings.evidence_dual_channel_minimum == 0.20
    assert settings.query_variant_max_count == 3
    assert settings.retrieval_allow_vector_only_fallback is False
    assert settings.evidence_semantic_threshold != settings.evidence_threshold


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retrieval_lexical_candidate_limit", 0),
        ("retrieval_vector_candidate_limit", 101),
        ("retrieval_fused_top_k", 0),
        ("evidence_max_chunks", 13),
        ("evidence_lexical_threshold", 1.1),
        ("evidence_semantic_threshold", -0.1),
        ("evidence_dual_channel_minimum", 1.1),
        ("query_variant_max_count", 4),
    ],
)
def test_rag_retrieval_settings_reject_unsafe_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        valid_settings(**{field: value})

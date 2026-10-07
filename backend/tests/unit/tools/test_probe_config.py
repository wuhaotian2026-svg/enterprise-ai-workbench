from __future__ import annotations

from policy_api.tools.probe_config import ProbeSettings


def test_probe_settings_require_only_the_model_provider_contract(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("MODEL_API_KEY", "probe-secret")
    monkeypatch.setenv("CHAT_MODEL", "chat-test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    settings = ProbeSettings(_env_file=None)

    assert str(settings.model_base_url) == "https://models.example.test/v1"
    assert settings.model_api_key.get_secret_value() == "probe-secret"
    assert settings.chat_model == "chat-test"
    assert settings.model_timeout_seconds == 30
    assert "probe-secret" not in repr(settings)

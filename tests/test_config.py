from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ConfigError

UNSET = {"anthropic_api_key": None, "ic_app_token": None, "ten_analytics_endpoint": None, "ten_analytics_api_key": None}


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **{**UNSET, **overrides})


def test_production_requires_model_key_and_access_token():
    with pytest.raises(ConfigError) as info:
        make(app_env="production").validate_startup()
    assert "ANTHROPIC_API_KEY" in str(info.value) and "IC_APP_TOKEN" in str(info.value)


def test_production_heuristic_mode_needs_only_the_token():
    settings = make(app_env="production", llm_enabled=False, ic_app_token=SecretStr("x" * 24))
    warnings = settings.validate_startup()
    assert [w for w in warnings if "RESEND_API_KEY" not in w] == []  # email is optional
    assert settings.cookie_secure and not settings.llm_available


def test_short_access_token_is_rejected():
    with pytest.raises(ConfigError, match="at least 16"):
        make(app_env="production", anthropic_api_key=SecretStr("k"), ic_app_token=SecretStr("short")).validate_startup()


def test_analytics_endpoint_validation():
    with pytest.raises(ConfigError, match="TEN_ANALYTICS_API_KEY is required"):
        make(ten_analytics_endpoint="https://example.test/a").validate_startup()
    with pytest.raises(ConfigError, match="https in production"):
        make(app_env="production", llm_enabled=False, ic_app_token=SecretStr("x" * 20),
             ten_analytics_endpoint="http://example.test/a", ten_analytics_api_key=SecretStr("k")).validate_startup()
    with pytest.raises(ConfigError, match="absolute"):
        make(ten_analytics_endpoint="example.test/a", ten_analytics_api_key=SecretStr("k")).validate_startup()


def test_blank_environment_values_are_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("IC_APP_TOKEN", "  ")
    monkeypatch.setenv("MAX_UPLOAD_MB", "10")
    settings = Settings(_env_file=None)
    assert settings.anthropic_api_key is None and settings.ic_app_token is None
    assert settings.max_upload_bytes == 10 * 1024 * 1024


def test_development_warns_about_degraded_modes():
    warnings = make(app_env="development").validate_startup()
    assert any("heuristic mode" in w for w in warnings) and any("IC_APP_TOKEN" in w for w in warnings)


def test_secrets_do_not_leak_in_repr():
    settings = make(anthropic_api_key=SecretStr("sk-ant-secret-value"))
    assert "sk-ant-secret-value" not in repr(settings)


def test_upload_root_is_created(tmp_path):
    root = make(upload_dir=tmp_path / "private").upload_root()
    assert root.is_dir()

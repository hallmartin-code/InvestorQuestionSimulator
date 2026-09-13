"""Application configuration from environment variables, validated at startup.

Secrets are read only from the environment (or a local, git-ignored ``.env``) and held as
``SecretStr`` so they never appear in logs or reprs.
"""

from __future__ import annotations

import re
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigError

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
LOGO_PATH = ASSETS_DIR / "TEN_Capital_logo_footer.png"
WEB_DIR = APP_DIR / "web"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

MIN_TOKEN_LENGTH = 16
_EMAIL = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,255}\.[A-Za-z]{2,}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "production", "test"] = "development"

    # --- Secrets and external services ------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    ic_app_token: SecretStr | None = None
    ten_analytics_endpoint: str | None = None
    ten_analytics_api_key: SecretStr | None = None

    # --- Results email (Resend) -------------------------------------------------------
    resend_api_key: SecretStr | None = None
    email_enabled: bool = True
    #: Comma-separated recipients. Configuration only; never taken from user input.
    email_to: str = "Info@tencapital.group"
    #: Must be on a domain verified in Resend.
    email_from: str = "TEN Capital Interview Simulator <reports@tencapital.group>"
    email_reply_to: str | None = None
    email_attach_pdf: bool = True
    #: The session JSON is attached only when the founder also granted data-storage consent.
    email_attach_json: bool = True
    email_requires_storage_consent: bool = False
    email_timeout_seconds: float = Field(15.0, gt=0, le=60)

    # --- Uploads and retention --------------------------------------------------------
    max_upload_mb: int = Field(25, ge=1, le=100)
    data_retention_hours: float = Field(24.0, gt=0, le=720)
    upload_dir: Path | None = None

    # --- Model ------------------------------------------------------------------------
    llm_enabled: bool = True
    llm_model: str = "claude-opus-5"
    llm_effort: Effort = "high"
    llm_question_effort: Effort = "medium"
    llm_timeout_seconds: float = Field(240.0, gt=0)
    llm_max_retries: int = Field(2, ge=0, le=5)
    llm_use_fallbacks: bool = True

    # --- Interview --------------------------------------------------------------------
    interview_max_questions: int = Field(12, ge=3, le=30)
    interview_max_followups_per_topic: int = Field(2, ge=0, le=4)

    # --- Web --------------------------------------------------------------------------
    session_cookie_secure: bool | None = None
    analytics_timeout_seconds: float = Field(10.0, gt=0, le=60)
    log_level: str = "INFO"

    @field_validator(
        "anthropic_api_key", "ic_app_token", "ten_analytics_endpoint", "ten_analytics_api_key",
        "upload_dir", "session_cookie_secure", "resend_api_key", "email_reply_to", mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- Derived ----------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def retention_seconds(self) -> float:
        return self.data_retention_hours * 3600

    @property
    def llm_available(self) -> bool:
        return self.llm_enabled and self.anthropic_api_key is not None

    @property
    def analytics_configured(self) -> bool:
        return bool(self.ten_analytics_endpoint and self.ten_analytics_api_key)

    @property
    def email_recipients(self) -> list[str]:
        return [address.strip() for address in self.email_to.split(",") if address.strip()]

    @property
    def email_configured(self) -> bool:
        return self.email_enabled and self.resend_api_key is not None and bool(self.email_recipients)

    @property
    def access_token_required(self) -> bool:
        return self.ic_app_token is not None

    @property
    def cookie_secure(self) -> bool:
        return self.is_production if self.session_cookie_secure is None else self.session_cookie_secure

    def upload_root(self) -> Path:
        """The restricted directory uploads are parsed in; created owner-only on first use."""
        root = self.upload_dir or Path(tempfile.gettempdir()) / "ten-iqs-uploads"
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:  # pragma: no cover - some filesystems ignore POSIX modes
            pass
        return root

    def validate_startup(self) -> list[str]:
        """Raise ``ConfigError`` for unusable configuration; return warnings for degraded modes."""
        errors: list[str] = []
        warnings: list[str] = []

        if self.is_production:
            if self.llm_enabled and self.anthropic_api_key is None:
                errors.append("ANTHROPIC_API_KEY is required in production "
                              "(or set LLM_ENABLED=false to run in heuristic mode).")
            if self.ic_app_token is None:
                errors.append("IC_APP_TOKEN is required in production so the app is not publicly accessible.")
            elif len(self.ic_app_token.get_secret_value()) < MIN_TOKEN_LENGTH:
                errors.append(f"IC_APP_TOKEN must be at least {MIN_TOKEN_LENGTH} characters.")
        else:
            if not self.llm_available:
                warnings.append("ANTHROPIC_API_KEY is not set or LLM_ENABLED=false: the interview runs in "
                                "heuristic mode (rule-based extraction, template questions, heuristic scoring).")
            if self.ic_app_token is None:
                warnings.append("IC_APP_TOKEN is not set: the app is open to anyone who can reach it.")

        if self.ten_analytics_endpoint:
            parsed = urlparse(self.ten_analytics_endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append("TEN_ANALYTICS_ENDPOINT must be an absolute http(s) URL.")
            elif parsed.scheme != "https" and self.is_production:
                errors.append("TEN_ANALYTICS_ENDPOINT must use https in production.")
            if self.ten_analytics_api_key is None:
                errors.append("TEN_ANALYTICS_API_KEY is required when TEN_ANALYTICS_ENDPOINT is set.")
        elif self.ten_analytics_api_key is not None:
            warnings.append("TEN_ANALYTICS_API_KEY is set without TEN_ANALYTICS_ENDPOINT; analytics are disabled.")

        if self.email_enabled:
            if self.resend_api_key is None:
                warnings.append("RESEND_API_KEY is not set: results emails are disabled.")
            else:
                if not self.email_recipients:
                    errors.append("EMAIL_TO must list at least one recipient when RESEND_API_KEY is set.")
                bad = [a for a in self.email_recipients if not _EMAIL.match(a)]
                if bad:
                    errors.append(f"EMAIL_TO contains invalid addresses: {', '.join(bad)}.")
                sender = self.email_from.rsplit("<", 1)[-1].rstrip(">").strip()
                if not _EMAIL.match(sender):
                    errors.append("EMAIL_FROM must be an address (optionally 'Name <address>') on a Resend-verified domain.")
                if self.email_reply_to and not _EMAIL.match(self.email_reply_to):
                    errors.append("EMAIL_REPLY_TO must be a valid address.")

        if errors:
            raise ConfigError("Invalid configuration:\n- " + "\n- ".join(errors))
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

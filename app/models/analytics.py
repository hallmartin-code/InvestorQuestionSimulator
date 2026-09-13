"""Consent, user identity and the analytics delivery result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class ConsentSettings(BaseModel):
    """Independent permissions. Granting one never implies another.

    * ai_processing_consent - deck text and answers may be sent to Anthropic's API for
      extraction, question generation, scoring and coaching. Without it the session runs in
      heuristic mode and nothing leaves the server.
    * analytics_consent - an anonymized session analytics record may be sent to TEN Capital
      (sector, stage, scores, engagement; no names, emails, company name or financials).
    * data_storage_consent - TEN Capital may also store confidential company details in that
      record (company name, geography, raise, valuation, runway).
    * marketing_consent - TEN Capital may also receive the user's name and email to follow up.
    """

    ai_processing_consent: bool = False
    analytics_consent: bool = False
    data_storage_consent: bool = False
    marketing_consent: bool = False
    captured_at: datetime = Field(default_factory=_now)

    def for_record(self) -> dict[str, bool]:
        return {
            "analytics_consent": self.analytics_consent,
            "data_storage_consent": self.data_storage_consent,
            "marketing_consent": self.marketing_consent,
        }


class UserInfo(BaseModel):
    user_id: str | None = None
    name: str | None = None
    email: str | None = None


class AnalyticsResult(BaseModel):
    status: Literal["sent", "skipped", "failed"]
    detail: str
    attempted_at: datetime = Field(default_factory=_now)


class EmailResult(BaseModel):
    status: Literal["sent", "skipped", "failed"]
    detail: str
    message_id: str | None = None
    recipients: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    attempted_at: datetime = Field(default_factory=_now)

"""TEN Capital session analytics: consent-filtered record construction and fail-safe delivery.

Consent matrix (each permission is independent and additive):

=====================  ===============================================================
Consent                What the record may contain
=====================  ===============================================================
none                   nothing is built or sent
analytics_consent      sector, subsector, stage, interview scores and topics, engagement
+ data_storage         company name, geography, raise, committed, valuation, instrument,
                       runway (confidential deck information)
+ marketing            user name, email and a pseudonymous user id
=====================  ===============================================================

Deck text and founder answers are never part of the record. Delivery failures are reported
as a result object and never raised: analytics must not block the founder's assessment.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from app import __version__
from app.config import Settings
from app.models.analysis import IssueTag
from app.models.analytics import AnalyticsResult
from app.utils.logging import get_logger
from app.utils.text import truncate

if TYPE_CHECKING:  # pragma: no cover
    from app.session import InterviewSession

LOGGER = get_logger(__name__)


def _pseudonymous_id(email: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if not email:
        return None
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:24]


def build_analytics_record(session: InterviewSession, *, now: datetime | None = None) -> dict[str, Any] | None:
    consent = session.consent
    if not consent.analytics_consent:
        return None
    profile = session.profile
    state = session.interview
    assessment = session.assessment
    readiness = assessment.readiness if assessment else None

    def field(name: str) -> Any:
        return profile.value(name) if profile is not None else None

    confidential = consent.data_storage_consent
    identified = consent.marketing_consent

    red_flag_topics: list[str] = []
    if session.gaps is not None:
        for issue in session.gaps.issues:
            label = issue.category.label
            if issue.tag is IssueTag.RED_FLAG and label not in red_flag_topics:
                red_flag_topics.append(label)
    highest_risk = [readiness.highest_risk_topic] if readiness and readiness.highest_risk_topic else []
    highest_risk += [t for t in red_flag_topics if t not in highest_risk]

    return {
        "session_id": session.analytics_id,
        "timestamp": (now or datetime.now(UTC)).isoformat(),
        "user": {
            "user_id": _pseudonymous_id(session.user.email, session.user.user_id) if identified else None,
            "name": session.user.name if identified else None,
            "email": session.user.email if identified else None,
        },
        "company": {
            "name": field("company_name") if confidential else None,
            "sector": truncate(str(field("sector") or ""), 60) or None,
            "subsector": truncate(str(field("subsector") or ""), 60) or None,
            "stage": truncate(str(field("stage") or ""), 40) or None,
            "geography": field("geography") if confidential else None,
        },
        "raise": {
            "amount": field("raise_amount") if confidential else None,
            "amount_committed": field("amount_committed") if confidential else None,
            "valuation": field("valuation") if confidential else None,
            "instrument": field("instrument") if confidential else None,
            "runway_months": field("runway_months") if confidential else None,
        },
        "interview": {
            "questions_asked": state.answered if state else 0,
            "average_answer_score": readiness.average_answer_score if readiness and readiness.average_answer_score is not None else 0,
            "readiness_score": readiness.overall_score if readiness and readiness.overall_score is not None else 0,
            "readiness_level": readiness.level.value if readiness and readiness.level else "",
            "strong_topics": [a.label for a in assessment.strongest_areas] if assessment else [],
            "weak_topics": [a.label for a in assessment.weakest_areas] if assessment else [],
            "highest_risk_topics": highest_risk,
        },
        "engagement": {
            "session_completed": bool(state and state.completed and not state.ended_early),
            "duration_seconds": session.duration_seconds,
        },
        "consent": consent.for_record(),
    }


def send_analytics(record: dict[str, Any], settings: Settings, *, client: httpx.Client | None = None) -> AnalyticsResult:
    if not settings.analytics_configured:
        return AnalyticsResult(status="skipped", detail="TEN Capital analytics endpoint is not configured.")
    assert settings.ten_analytics_endpoint and settings.ten_analytics_api_key  # narrowed by analytics_configured
    headers = {
        "Authorization": f"Bearer {settings.ten_analytics_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        "User-Agent": f"ten-interview-simulator/{__version__}",
    }
    owns_client = client is None
    http = client or httpx.Client(timeout=settings.analytics_timeout_seconds)
    detail = "Analytics delivery failed."
    try:
        for attempt in range(2):
            try:
                response = http.post(settings.ten_analytics_endpoint, json=record, headers=headers,
                                     timeout=settings.analytics_timeout_seconds)
            except httpx.HTTPError as exc:
                detail = f"Analytics endpoint unreachable ({exc.__class__.__name__})."
                continue
            if 200 <= response.status_code < 300:
                return AnalyticsResult(status="sent", detail="Session analytics shared with TEN Capital.")
            detail = f"Analytics endpoint returned HTTP {response.status_code}."
            if response.status_code < 500:
                break
            LOGGER.info("Analytics attempt %d failed with HTTP %d", attempt + 1, response.status_code)
    except Exception as exc:  # noqa: BLE001 - analytics must never break the session
        detail = f"Analytics delivery failed ({exc.__class__.__name__})."
    finally:
        if owns_client:
            http.close()
    LOGGER.warning("Analytics not delivered: %s", detail)
    return AnalyticsResult(status="failed", detail=detail)

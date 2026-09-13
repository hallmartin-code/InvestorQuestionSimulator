from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.integrations.ten_analytics import build_analytics_record, send_analytics
from app.models.analytics import ConsentSettings, UserInfo
from app.pipeline import InterviewService
from app.session import SessionStore
from tests.conftest import GOOD_ANSWER, run_interview, upload

ENDPOINT = "https://analytics.example.test/sessions"


def analytics_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"ten_analytics_endpoint": ENDPOINT,
                                       "ten_analytics_api_key": SecretStr("test-analytics-key")})


class Recorder:
    def __init__(self, status: int = 202, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            return httpx.Response(self.status)

        return httpx.Client(transport=httpx.MockTransport(handler))


def finished(settings, fixture_files, consent: ConsentSettings, recorder: Recorder | None = None, *, limit=None):
    service = InterviewService(analytics_settings(settings), None,
                               analytics_client=recorder.client() if recorder else None)
    session = SessionStore(3600).create()
    upload(service, session, fixture_files, consent=consent,
           user=UserInfo(name="Dana Reyes", email="dana@careloop.example"))
    run_interview(service, session, limit=limit)
    service.finish_interview(session)
    return service, session


def test_no_consent_sends_nothing(settings, fixture_files):
    recorder = Recorder()
    _service, session = finished(settings, fixture_files, ConsentSettings(), recorder)
    assert build_analytics_record(session) is None
    assert session.analytics.status == "skipped"
    assert recorder.requests == []


def test_analytics_only_consent_excludes_pii_and_confidential_data(settings, fixture_files):
    recorder = Recorder()
    _service, session = finished(settings, fixture_files, ConsentSettings(analytics_consent=True), recorder)
    assert session.analytics.status == "sent"
    body = json.loads(recorder.requests[0].content)
    assert set(body) == {"session_id", "timestamp", "user", "company", "raise", "interview", "engagement", "consent"}
    assert body["user"] == {"user_id": None, "name": None, "email": None}
    assert body["company"]["name"] is None and body["company"]["geography"] is None
    assert body["company"]["sector"] == "Healthcare"
    assert all(value is None for value in body["raise"].values())
    assert isinstance(body["interview"]["readiness_score"], int) and body["interview"]["questions_asked"] > 0
    assert body["consent"] == {"analytics_consent": True, "data_storage_consent": False, "marketing_consent": False}
    assert body["session_id"] != session.session_id
    serialized = recorder.requests[0].content.decode()
    assert "CareLoop" not in serialized and GOOD_ANSWER[:40] not in serialized and "dana@" not in serialized
    assert recorder.requests[0].headers["Authorization"] == "Bearer test-analytics-key"


def test_storage_and_marketing_consent_add_permitted_fields(settings, fixture_files):
    _service, session = finished(settings, fixture_files,
                                 ConsentSettings(analytics_consent=True, data_storage_consent=True), Recorder())
    record = build_analytics_record(session)
    assert record["company"]["name"] == "CareLoop Health"
    assert record["raise"]["amount"] == 4_000_000 and record["raise"]["instrument"] == "SAFE"
    assert record["user"]["email"] is None

    _service, session = finished(settings, fixture_files, ConsentSettings(
        analytics_consent=True, marketing_consent=True), Recorder())
    record = build_analytics_record(session)
    assert record["user"]["email"] == "dana@careloop.example" and record["user"]["name"] == "Dana Reyes"
    assert record["user"]["user_id"] and "@" not in record["user"]["user_id"]
    assert record["company"]["name"] is None


@pytest.mark.parametrize(("recorder", "detail"), [
    (Recorder(status=500), "HTTP 500"),
    (Recorder(error=httpx.ConnectError("refused")), "unreachable"),
])
def test_endpoint_failure_never_blocks_the_assessment(settings, fixture_files, recorder, detail):
    _service, session = finished(settings, fixture_files, ConsentSettings(analytics_consent=True), recorder)
    assert session.stage == "results" and session.assessment is not None
    assert session.analytics.status == "failed" and detail in session.analytics.detail
    assert len(recorder.requests) == 2  # one retry


def test_client_error_is_not_retried(settings):
    recorder = Recorder(status=401)
    result = send_analytics({"x": 1}, analytics_settings(settings), client=recorder.client())
    assert result.status == "failed" and len(recorder.requests) == 1


def test_unconfigured_endpoint_is_skipped(settings):
    assert send_analytics({"x": 1}, settings).status == "skipped"


def test_incomplete_session_is_reported_as_not_completed(settings, fixture_files):
    _service, session = finished(settings, fixture_files, ConsentSettings(analytics_consent=True), Recorder(), limit=2)
    record = build_analytics_record(session)
    assert record["engagement"]["session_completed"] is False
    assert record["engagement"]["duration_seconds"] is not None
    assert record["interview"]["questions_asked"] == 2

from __future__ import annotations

import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ConfigError
from app.integrations.email_notifier import RESEND_ENDPOINT, send_results_email
from app.models.analytics import ConsentSettings, UserInfo
from app.pipeline import InterviewService
from app.session import SessionStore
from tests.conftest import GOOD_ANSWER, run_interview, upload

TEST_KEY = "resend-test-key"


def email_settings(settings: Settings, **overrides) -> Settings:
    return settings.model_copy(update={"resend_api_key": SecretStr(TEST_KEY), **overrides})


class Resend:
    """Records requests to a mocked Resend API."""

    def __init__(self, status: int = 200, body: dict | None = None, error: Exception | None = None) -> None:
        self.status, self.body, self.error = status, body or {"id": "email_123"}, error
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            return httpx.Response(self.status, json=self.body)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def payload(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)


def finish(settings, fixture_files, resend: Resend, *, consent=None, user=None, inputs=None, limit=None):
    service = InterviewService(settings, None, email_client=resend.client())
    session = SessionStore(3600).create()
    upload(service, session, fixture_files, consent=consent or ConsentSettings(), user=user or UserInfo(),
           inputs=inputs)
    run_interview(service, session, limit=limit)
    service.finish_interview(session)
    return session


def test_every_finished_interview_is_emailed_to_the_info_inbox(settings, fixture_files):
    resend = Resend()
    session = finish(email_settings(settings), fixture_files, resend)
    assert len(resend.requests) == 1
    request = resend.requests[0]
    assert str(request.url) == RESEND_ENDPOINT
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert request.headers["Idempotency-Key"] == f"interview-results-{session.analytics_id}"

    payload = resend.payload()
    assert payload["to"] == ["Info@tencapital.group"]
    assert payload["from"].endswith("@tencapital.group>")
    score = session.assessment.readiness.overall_score
    assert payload["subject"].startswith("[Interview Readiness] CareLoop Health — ")
    assert f"{score}/100" in payload["subject"]
    for section in ("INVESTOR ASSESSMENT", "CATEGORY SCORES", "PRACTICE PLAN", "FUNDRAISING SNAPSHOT", "INTERVIEW",
                    "CONSENT"):
        assert section in payload["text"]
    assert "Investor assessment" in payload["html"] and "$4M" in payload["html"]

    attachments = {a["filename"]: base64.b64decode(a["content"]) for a in payload["attachments"]}
    assert list(attachments) == ["Fundraising_Interview_Readiness_CareLoop_Health.pdf"]
    assert attachments["Fundraising_Interview_Readiness_CareLoop_Health.pdf"].startswith(b"%PDF")

    assert session.email.status == "sent" and session.email.message_id == "email_123"
    assert session.email.attachments == ["Fundraising_Interview_Readiness_CareLoop_Health.pdf"]


def test_answers_and_contact_details_require_consent(settings, fixture_files):
    user = UserInfo(name="Dana Reyes", email="dana@careloop.example")
    resend = Resend()
    finish(email_settings(settings), fixture_files, resend, user=user)
    payload = resend.payload()
    assert "dana@careloop.example" not in payload["html"] + payload["text"]
    assert "Withheld" in payload["text"]
    assert GOOD_ANSWER[:40] not in payload["html"] + payload["text"]
    assert len(payload["attachments"]) == 1

    resend = Resend()
    consent = ConsentSettings(analytics_consent=True, data_storage_consent=True, marketing_consent=True)
    finish(email_settings(settings), fixture_files, resend, consent=consent, user=user)
    payload = resend.payload()
    assert "dana@careloop.example" in payload["text"]
    names = [a["filename"] for a in payload["attachments"]]
    assert names[1] == "Fundraising_Interview_Readiness_CareLoop_Health_session.json"
    export = json.loads(base64.b64decode(payload["attachments"][1]["content"]))
    assert export["interview"]["responses"][0]["founder_answer"] == GOOD_ANSWER


def test_user_supplied_text_is_escaped_in_html(settings, fixture_files):
    resend = Resend()
    finish(email_settings(settings), fixture_files, resend, inputs={"company_name": "<img src=x onerror=alert(1)>"})
    html = resend.payload()["html"]
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_optional_attachments_and_recipient_list(settings, fixture_files):
    resend = Resend()
    configured = email_settings(settings, email_attach_pdf=False, email_to="Info@tencapital.group, deals@tencapital.group",
                                email_reply_to="deals@tencapital.group")
    session = finish(configured, fixture_files, resend)
    payload = resend.payload()
    assert payload["to"] == ["Info@tencapital.group", "deals@tencapital.group"]
    assert payload["reply_to"] == "deals@tencapital.group"
    assert "attachments" not in payload and session.email.status == "sent"


@pytest.mark.parametrize(("resend", "attempts", "detail"), [
    (Resend(status=500, body={"message": "internal"}), 2, "HTTP 500"),
    (Resend(status=422, body={"message": "The tencapital.group domain is not verified."}), 1, "not verified"),
    (Resend(error=httpx.ConnectError("refused")), 2, "could not be reached"),
])
def test_delivery_failures_never_block_results(settings, fixture_files, resend, attempts, detail):
    session = finish(email_settings(settings), fixture_files, resend)
    assert session.stage == "results" and session.assessment is not None
    assert session.email.status == "failed" and detail in session.email.detail
    assert len(resend.requests) == attempts


def test_email_is_skipped_when_not_configured_or_not_permitted(settings, fixture_files):
    resend = Resend()
    session = finish(settings, fixture_files, resend)
    assert session.email.status == "skipped" and "RESEND_API_KEY" in session.email.detail

    session = finish(email_settings(settings, email_enabled=False), fixture_files, resend)
    assert session.email.status == "skipped"

    session = finish(email_settings(settings, email_requires_storage_consent=True), fixture_files, resend)
    assert session.email.status == "skipped" and "consent" in session.email.detail
    assert resend.requests == []

    session = finish(email_settings(settings, email_requires_storage_consent=True), fixture_files, resend,
                     consent=ConsentSettings(data_storage_consent=True))
    assert session.email.status == "sent"


def test_incomplete_interviews_are_emailed_with_their_status(settings, fixture_files):
    resend = Resend()
    session = finish(email_settings(settings), fixture_files, resend, limit=1)
    payload = resend.payload()
    assert session.interview.ended_early
    assert "(provisional)" in payload["subject"] and "ended early" in payload["text"]


def test_results_page_reports_email_status(settings, fixture_files):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_web import csrf_from, post_upload

    resend = Resend()
    client = TestClient(create_app(email_settings(settings), email_client=resend.client()))
    token = csrf_from(client)
    assert "emailed to TEN Capital" in client.get("/").text
    post_upload(client, fixture_files, token)
    client.post("/review", data={"csrf_token": token})
    import re

    question_id = re.search(r'name="question_id" value="([^"]+)"', client.get("/interview").text).group(1)
    client.post("/interview/answer", data={"csrf_token": token, "question_id": question_id, "answer": GOOD_ANSWER})
    client.post("/interview/finish", data={"csrf_token": token})
    assert "Results email: sent to TEN Capital" in client.get("/results").text
    assert len(resend.requests) == 1


def test_email_configuration_is_validated():
    base = {"anthropic_api_key": None, "ic_app_token": None, "ten_analytics_endpoint": None,
            "ten_analytics_api_key": None}
    with pytest.raises(ConfigError, match="EMAIL_TO contains invalid"):
        Settings(_env_file=None, **base, resend_api_key=SecretStr("k"), email_to="not-an-address").validate_startup()
    with pytest.raises(ConfigError, match="EMAIL_FROM"):
        Settings(_env_file=None, **base, resend_api_key=SecretStr("k"), email_from="TEN Capital").validate_startup()
    warnings = Settings(_env_file=None, **base, resend_api_key=None).validate_startup()
    assert any("RESEND_API_KEY" in w for w in warnings)
    ok = Settings(_env_file=None, **base, resend_api_key=SecretStr("k"),
                  email_from="TEN Capital Interview Simulator <reports@tencapital.group>")
    ok.validate_startup()
    assert ok.email_configured and ok.email_recipients == ["Info@tencapital.group"]


def test_sender_never_raises_on_unexpected_errors(settings, completed_session):
    class Broken:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("boom")

        def close(self):
            pass

    result = send_results_email(completed_session, email_settings(settings), pdf=None, pdf_name="r.pdf", export=None,
                                client=Broken())
    assert result.status == "failed" and "RuntimeError" in result.detail

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from tests.conftest import GOOD_ANSWER

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
QUESTION_ID = re.compile(r'name="question_id" value="([^"]+)"')
TOKEN = "correct-horse-battery-staple"


def make_client(settings, **overrides) -> TestClient:
    return TestClient(create_app(settings.model_copy(update=overrides)))


def csrf_from(client: TestClient, path: str = "/") -> str:
    match = CSRF.search(client.get(path).text)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def post_upload(client: TestClient, files, token: str, *, key: str = "sample_pdf", filename: str | None = None,
                content_type: str = "application/pdf", **fields):
    path = files[key]
    data = {"csrf_token": token, "confirm_rights": "on", **fields}
    return client.post("/upload", data=data, follow_redirects=False,
                       files={"deck": (filename or path.name, path.read_bytes(), content_type)})


def test_full_browser_flow(settings, fixture_files):
    client = make_client(settings)
    token = csrf_from(client)

    response = post_upload(client, fixture_files, token, company_name="", raise_amount="")
    assert response.status_code == 303 and response.headers["location"] == "/review"
    review = client.get("/review")
    assert "CareLoop Health" in review.text and "From deck" in review.text and "Slide 10" in review.text

    response = client.post("/review", data={"csrf_token": token, "field__raise_amount": "$4.5M"}, follow_redirects=False)
    assert response.headers["location"] == "/interview"
    page = client.get("/interview")
    question_id = QUESTION_ID.search(page.text).group(1)
    assert "Level 1" in page.text

    response = client.post("/interview/answer", data={"csrf_token": token, "question_id": question_id,
                                                      "answer": GOOD_ANSWER}, follow_redirects=False)
    assert response.headers["location"] == "/interview/feedback"
    feedback = client.get("/interview/feedback")
    for section in ("Investor assessment", "What worked", "What needs improvement", "Better answer structure",
                    "Model answer", "/20"):
        assert section in feedback.text
    assert QUESTION_ID.search(feedback.text).group(1) != question_id  # the next question is active

    response = client.post("/interview/finish", data={"csrf_token": token}, follow_redirects=False)
    assert response.headers["location"] == "/results"
    results = client.get("/results")
    assert "Practice plan" in results.text and "Category scores" in results.text

    pdf = client.get("/results/report.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert "attachment" in pdf.headers["content-disposition"] and "CareLoop_Health" in pdf.headers["content-disposition"]
    export = client.get("/results/session.json").json()
    assert export["company_profile"]["fundraising"]["raise_amount"] == 4_500_000


def test_state_changing_requests_require_csrf(settings, fixture_files):
    client = make_client(settings)
    client.get("/")
    response = post_upload(client, fixture_files, "forged-token")
    assert response.status_code == 403


def test_stage_guards_redirect(settings):
    client = make_client(settings)
    for path in ("/review", "/interview", "/interview/feedback", "/results", "/results/report.pdf"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"


def test_unsupported_file_shows_an_actionable_error(settings, fixture_files):
    client = make_client(settings)
    token = csrf_from(client)
    response = post_upload(client, fixture_files, token, filename="notes.txt", content_type="text/plain")
    assert response.status_code == 422
    assert "unsupported file type" in response.text


def test_upload_requires_rights_confirmation(settings, fixture_files):
    client = make_client(settings)
    token = csrf_from(client)
    path = fixture_files["sample_pdf"]
    response = client.post("/upload", data={"csrf_token": token}, files={"deck": (path.name, path.read_bytes(), "application/pdf")})
    assert response.status_code == 400 and "authorized" in response.text


def test_oversized_request_is_rejected_before_parsing(settings):
    client = make_client(settings, max_upload_mb=1)
    token = csrf_from(client)
    response = client.post("/upload", data={"csrf_token": token},
                           files={"deck": ("big.pdf", b"%PDF-" + b"0" * (3 * 1024 * 1024), "application/pdf")})
    assert response.status_code == 413


def test_access_token_gate_and_session_rotation(settings):
    client = make_client(settings, ic_app_token=SecretStr(TOKEN))
    response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/login"
    token = csrf_from(client, "/login")
    assert client.post("/login", data={"csrf_token": token, "access_token": "wrong"}).status_code == 401
    before = client.cookies.get("iqs_session")
    response = client.post("/login", data={"csrf_token": token, "access_token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    assert client.cookies.get("iqs_session") != before
    assert client.get("/", follow_redirects=False).status_code == 200


def test_security_headers_and_cookie_flags(settings):
    client = make_client(settings)
    response = client.get("/")
    headers = response.headers
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    cookie = headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "set-cookie" not in client.get("/health").headers


def test_reset_starts_a_new_session(settings, fixture_files):
    client = make_client(settings)
    token = csrf_from(client)
    post_upload(client, fixture_files, token)
    before = client.cookies.get("iqs_session")
    response = client.post("/reset", data={"csrf_token": token}, follow_redirects=False)
    assert response.headers["location"] == "/"
    assert client.cookies.get("iqs_session") != before
    assert client.get("/review", follow_redirects=False).headers["location"] == "/"


def test_self_hosted_design_assets_are_served(settings):
    client = make_client(settings)
    page = client.get("/").text
    assert "brand-mark" in page and "data-dropzone" in page and "fonts.googleapis.com" not in page
    for font in ("sora-latin.woff2", "inter-latin.woff2", "jetbrains-mono-latin.woff2"):
        response = client.get(f"/static/fonts/{font}")
        assert response.status_code == 200 and response.content[:4] == b"wOF2"
        assert response.headers["content-type"] == "font/woff2"


def test_favicon_set_is_linked_and_served(settings):
    import json
    from io import BytesIO

    from PIL import Image

    client = make_client(settings)
    page = client.get("/").text
    for href in ("favicon.ico", "favicon-32x32.png", "favicon-16x16.png", "apple-touch-icon.png", "site.webmanifest"):
        assert f"/static/{href}" in page, href

    root = client.get("/favicon.ico")
    assert root.status_code == 200 and root.headers["content-type"] == "image/x-icon"
    assert "set-cookie" not in root.headers and "max-age" in root.headers["cache-control"]
    assert {(16, 16), (32, 32), (48, 48)} <= Image.open(BytesIO(root.content)).info["sizes"]

    for name, size in (("favicon-16x16.png", 16), ("favicon-32x32.png", 32), ("apple-touch-icon.png", 180),
                       ("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512)):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200 and Image.open(BytesIO(response.content)).size == (size, size), name

    manifest = client.get("/static/site.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    for icon in json.loads(manifest.content)["icons"]:
        assert client.get(icon["src"]).status_code == 200


@pytest.mark.parametrize("path", ["/health"])
def test_health(settings, path):
    response = make_client(settings).get(path)
    assert response.status_code == 200 and response.json()["status"] == "ok"

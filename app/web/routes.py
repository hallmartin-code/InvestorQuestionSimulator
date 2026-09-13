"""HTTP routes: upload → company review → interview/coaching → results.

Every state-changing request is a POST carrying the session's CSRF token. Heavy work runs in
the threadpool so the event loop stays responsive while the model is thinking.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from app import __version__
from app.analysis.company_profile import UPLOAD_FIELDS
from app.config import WEB_DIR, Settings
from app.errors import AppError, ReportError, SessionError
from app.models.analytics import ConsentSettings, UserInfo
from app.models.company import FIELD_SPECS, SECTION_LABELS
from app.pipeline import MAX_ANSWER_CHARS, InterviewService
from app.reports.report_data import report_filename
from app.session import InterviewSession, SessionStore
from app.utils.security import tokens_equal

router = APIRouter()

SESSION_COOKIE = "iqs_session"
STAGE_PATHS = {"upload": "/", "review": "/review", "interview": "/interview", "results": "/results"}
STEPS = [("upload", "Upload"), ("review", "Review"), ("interview", "Interview"), ("results", "Results")]
STAGE_OPTIONS = ["", "Pre-seed", "Seed", "Series A", "Series B", "Series C+"]
INSTRUMENT_OPTIONS = ["", "SAFE", "Convertible note", "Priced equity"]


class LoginThrottle:
    """Per-client failed-login limiter (in memory)."""

    def __init__(self, max_failures: int = 10, window_seconds: float = 900) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window_seconds
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def fail(self, key: str) -> int:
        recent = self._recent(key)
        recent.append(time.monotonic())
        return len(recent)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


# --- helpers -------------------------------------------------------------------------------

def _session(request: Request) -> InterviewSession:
    return request.state.session


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _service(request: Request) -> InterviewService:
    return request.app.state.service


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _render(request: Request, template: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    session = _session(request)
    settings = _settings(request)
    base = {
        "session": session,
        "csrf_token": session.csrf_token if session else "",
        "notices": session.pop_notices() if session else [],
        "steps": STEPS,
        "stage": session.stage if session else "upload",
        "ai_available": _service(request).ai_available,
        "max_upload_mb": settings.max_upload_mb,
        "retention_hours": settings.data_retention_hours,
        "results_emailed": settings.email_configured and not settings.email_requires_storage_consent,
        "email_recipients": settings.email_recipients,
        "results_emailed_with_storage": settings.email_configured and settings.email_requires_storage_consent,
        "version": __version__,
    }
    return request.app.state.templates.TemplateResponse(request, template, {**base, **context}, status_code=status_code)


def _gate(request: Request, *stages: str) -> Response | None:
    """Redirect to login or to the page for the session's actual stage."""
    session = _session(request)
    if _settings(request).access_token_required and not session.authenticated:
        return _redirect("/login")
    if session.stage not in stages:
        return _redirect(STAGE_PATHS[session.stage])
    return None


def _csrf_ok(request: Request, token: object) -> bool:
    return tokens_equal(_session(request).csrf_token, token if isinstance(token, str) else None)


def _forbidden(request: Request) -> HTMLResponse:
    return _render(request, "error.html", status_code=403, title="Form expired",
                   message="This form is out of date or was not submitted from this app. Reload the page and try again.")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- system ----------------------------------------------------------------------------------

@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Browsers request /favicon.ico at the site root regardless of the <link> tags."""
    return FileResponse(WEB_DIR / "static" / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=604800"})


@router.get("/login")
def login_page(request: Request) -> Response:
    if not _settings(request).access_token_required or _session(request).authenticated:
        return _redirect("/")
    return _render(request, "login.html")


@router.post("/login")
async def login(request: Request) -> Response:
    settings = _settings(request)
    session = _session(request)
    if not settings.access_token_required:
        return _redirect("/")
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf_token")):
        return _forbidden(request)
    throttle: LoginThrottle = request.app.state.throttle
    key = _client_key(request)
    if throttle.blocked(key):
        return _render(request, "login.html", status_code=429,
                       error="Too many failed attempts. Wait 15 minutes and try again.")
    token = form.get("access_token")
    assert settings.ic_app_token is not None
    if tokens_equal(settings.ic_app_token.get_secret_value(), token if isinstance(token, str) else None):
        throttle.reset(key)
        store: SessionStore = request.app.state.store
        store.rotate(session)
        session.authenticated = True
        return _redirect("/")
    failures = throttle.fail(key)
    await asyncio.sleep(min(failures, 5) * 0.3)
    return _render(request, "login.html", status_code=401, error="That access token is not valid.")


@router.post("/reset")
async def reset(request: Request) -> Response:
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf_token")):
        return _forbidden(request)
    old = _session(request)
    store: SessionStore = request.app.state.store
    store.delete(old.session_id)
    fresh = store.create()
    fresh.authenticated = old.authenticated
    request.state.session = fresh
    return _redirect("/")


# --- upload ----------------------------------------------------------------------------------------

@router.get("/")
def upload_page(request: Request) -> Response:
    if (redirect := _gate(request, "upload")) is not None:
        return redirect
    return _render(request, "upload.html", form={}, stage_options=STAGE_OPTIONS, instrument_options=INSTRUMENT_OPTIONS)


@router.post("/upload")
async def upload(request: Request) -> Response:
    if (redirect := _gate(request, "upload")) is not None:
        return redirect
    settings = _settings(request)
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf_token")):
        return _forbidden(request)

    values = {key: str(form.get(key) or "").strip() for key in (*UPLOAD_FIELDS, "notes", "user_name", "user_email")}
    checked = {key: form.get(key) == "on" for key in ("ai_processing_consent", "analytics_consent",
                                                      "data_storage_consent", "marketing_consent", "confirm_rights")}

    def fail(message: str, status_code: int = 400) -> HTMLResponse:
        return _render(request, "upload.html", status_code=status_code, error=message, form={**values, **checked},
                       stage_options=STAGE_OPTIONS, instrument_options=INSTRUMENT_OPTIONS)

    deck = form.get("deck")
    if not isinstance(deck, UploadFile) or not deck.filename:
        return fail("Choose a pitch deck (PDF, PPTX or PPT) to upload.")
    if not checked["confirm_rights"]:
        return fail("Confirm that you are authorized to share this deck.")
    data = await deck.read(settings.max_upload_bytes + 1)
    await deck.close()

    consent = ConsentSettings(
        ai_processing_consent=checked["ai_processing_consent"], analytics_consent=checked["analytics_consent"],
        data_storage_consent=checked["data_storage_consent"], marketing_consent=checked["marketing_consent"],
    )
    user = UserInfo(name=values["user_name"][:100] or None, email=values["user_email"][:254] or None)
    user_inputs = {key: values[key][:200] for key in UPLOAD_FIELDS}
    try:
        await run_in_threadpool(
            _service(request).start_session, _session(request), filename=deck.filename, data=data,
            content_type=deck.content_type, user_inputs=user_inputs, notes=values["notes"], consent=consent, user=user,
        )
    except AppError as exc:
        return fail(str(exc), 422 if not isinstance(exc, SessionError) else 409)
    finally:
        del data
    return _redirect("/review")


# --- review ------------------------------------------------------------------------------------------

@router.get("/review")
def review_page(request: Request) -> Response:
    if (redirect := _gate(request, "review")) is not None:
        return redirect
    session = _session(request)
    profile = session.profile
    assert profile is not None
    known = sum(1 for name in FIELD_SPECS if profile.get(name) is not None)
    return _render(request, "review.html", profile=profile, sections=profile.sections(), section_labels=SECTION_LABELS,
                   known=known, total=len(FIELD_SPECS), deck=session.deck)


@router.post("/review")
async def review(request: Request) -> Response:
    if (redirect := _gate(request, "review")) is not None:
        return redirect
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf_token")):
        return _forbidden(request)
    edits = {key[len("field__"):]: str(value) for key, value in form.items()
             if key.startswith("field__") and key[len("field__"):] in FIELD_SPECS}
    try:
        await run_in_threadpool(_service(request).confirm_profile, _session(request), edits)
    except AppError as exc:
        _session(request).add_notice(str(exc))
        return _redirect("/review")
    return _redirect("/interview")


# --- interview and coaching ----------------------------------------------------------------------------

@router.get("/interview")
def interview_page(request: Request) -> Response:
    if (redirect := _gate(request, "interview")) is not None:
        return redirect
    state = _session(request).interview
    return _render(request, "interview.html", state=state, question=state.current if state else None,
                   max_answer_chars=MAX_ANSWER_CHARS)


@router.post("/interview/answer")
async def submit_answer(request: Request) -> Response:
    if (redirect := _gate(request, "interview")) is not None:
        return redirect
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf_token")):
        return _forbidden(request)
    try:
        await run_in_threadpool(_service(request).submit_answer, _session(request),
                                question_id=str(form.get("question_id") or ""), answer=str(form.get("answer") or ""))
    except AppError as exc:
        _session(request).add_notice(str(exc))
        return _redirect("/interview")
    return _redirect("/interview/feedback")


@router.get("/interview/feedback")
def feedback_page(request: Request) -> Response:
    if (redirect := _gate(request, "interview")) is not None:
        return redirect
    session = _session(request)
    state = session.interview
    response = state.response_for(session.last_feedback_id) if state and session.last_feedback_id else None
    if response is None:
        return _redirect("/interview")
    return _render(request, "feedback.html", state=state, response=response, question=state.current,
                   step_reason=session.last_step_reason, max_answer_chars=MAX_ANSWER_CHARS)


@router.post("/interview/finish")
async def finish(request: Request) -> Response:
    if (redirect := _gate(request, "interview")) is not None:
        return redirect
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf_token")):
        return _forbidden(request)
    try:
        await run_in_threadpool(_service(request).finish_interview, _session(request))
    except AppError as exc:
        _session(request).add_notice(str(exc))
        return _redirect("/interview")
    return _redirect("/results")


# --- results -------------------------------------------------------------------------------------------

@router.get("/results")
def results_page(request: Request) -> Response:
    if (redirect := _gate(request, "results")) is not None:
        return redirect
    session = _session(request)
    return _render(request, "results.html", assessment=session.assessment, readiness=session.assessment.readiness
                   if session.assessment else None, profile=session.profile, state=session.interview,
                   analytics=session.analytics)


def _download_name(session: InterviewSession, extension: str) -> str:
    return report_filename(session.profile, extension)


@router.get("/results/report.pdf")
async def report_pdf(request: Request) -> Response:
    if (redirect := _gate(request, "results")) is not None:
        return redirect
    session = _session(request)
    try:
        pdf = await run_in_threadpool(_service(request).report_pdf, session)
    except ReportError as exc:
        session.add_notice(str(exc))
        return _redirect("/results")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_download_name(session, "pdf")}"'})


@router.get("/results/session.json")
def session_json(request: Request) -> Response:
    if (redirect := _gate(request, "results")) is not None:
        return redirect
    session = _session(request)
    return JSONResponse(_service(request).export_session(session),
                        headers={"Content-Disposition": f'attachment; filename="{_download_name(session, "json")}"'})

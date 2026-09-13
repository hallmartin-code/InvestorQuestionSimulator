"""FastAPI application factory, security middleware and startup validation.

Run locally:    uvicorn app.main:create_app --factory --reload
or:             python -m app.main
"""

from __future__ import annotations

import mimetypes
import os
import time
from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import WEB_DIR, Settings, get_settings
from app.integrations.llm_client import LLMClient, StructuredLLM
from app.pipeline import InterviewService
from app.session import SessionStore
from app.utils.logging import configure_logging, get_logger
from app.utils.security import purge_stale_files
from app.web.filters import register_filters
from app.web.routes import SESSION_COOKIE, LoginThrottle, router

LOGGER = get_logger(__name__)

# Slim container images ship without a MIME entry for self-hosted web fonts.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("application/manifest+json", ".webmanifest")

SESSIONLESS_PREFIXES = ("/static/", "/health", "/favicon.ico")
CACHEABLE_PREFIXES = ("/static/", "/favicon.ico")
PURGE_INTERVAL_SECONDS = 300
#: Parse workspaces exist for seconds; anything older in the upload root is an orphan.
ORPHAN_UPLOAD_SECONDS = 3600

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


def create_app(settings: Settings | None = None, *, llm: StructuredLLM | None = None,
               analytics_client: httpx.Client | None = None, email_client: httpx.Client | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    for warning in settings.validate_startup():
        LOGGER.warning(warning)
    if llm is None and settings.llm_available:
        llm = LLMClient(settings)

    app = FastAPI(title="TEN Capital Fundraising Interview Simulator", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = SessionStore(settings.retention_seconds)
    app.state.service = InterviewService(settings, llm, analytics_client=analytics_client, email_client=email_client)
    app.state.throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    register_filters(templates.env)
    app.state.templates = templates
    app.state.last_purge = time.monotonic()
    purge_stale_files(settings.upload_root(), ORPHAN_UPLOAD_SECONDS)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.middleware("http")
    async def session_and_security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > settings.max_upload_bytes + 1_048_576:
            return PlainTextResponse(f"Upload too large: the limit is {settings.max_upload_mb} MB.", status_code=413)

        store: SessionStore = request.app.state.store
        if time.monotonic() - request.app.state.last_purge > PURGE_INTERVAL_SECONDS:
            store.purge_expired()
            purge_stale_files(settings.upload_root(), ORPHAN_UPLOAD_SECONDS)
            request.app.state.last_purge = time.monotonic()

        cookie_id = request.cookies.get(SESSION_COOKIE)
        path = request.url.path
        session = None
        if not path.startswith(SESSIONLESS_PREFIXES):
            session = store.get(cookie_id) or store.create()
        request.state.session = session

        response = await call_next(request)

        current = getattr(request.state, "session", None)
        if current is not None and current.session_id != cookie_id:
            response.set_cookie(SESSION_COOKIE, current.session_id, httponly=True, secure=settings.cookie_secure,
                                samesite="lax", max_age=int(settings.retention_seconds), path="/")
        headers = response.headers
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        if not path.startswith(CACHEABLE_PREFIXES):
            headers["Cache-Control"] = "no-store"
        if settings.is_production:
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    app.include_router(router)
    return app


def run() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")), workers=1, proxy_headers=True)


if __name__ == "__main__":  # pragma: no cover
    run()

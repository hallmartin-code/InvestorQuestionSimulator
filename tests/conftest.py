from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from build_fixtures import build_all  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models.analytics import ConsentSettings, UserInfo  # noqa: E402
from app.pipeline import InterviewService  # noqa: E402
from app.session import InterviewSession, SessionStore  # noqa: E402

GOOD_ANSWER = (
    "We have 14 paying hospitals generating $1.2M ARR as of June 2025, up from $430K a year earlier. "
    "Net revenue retention is 112% because hospitals expand to more units, and 3 regional health systems "
    "have signed LOIs after their pilots, based on readmission data from each site."
)
WEAK_ANSWER = "We don't really calculate CAC yet."


class FakeLLM:
    """Test double for the model client: canned outputs per call label, with call recording."""

    available = True

    def __init__(self, responses: dict[str, Any] | None = None, *, fail: set[str] | None = None) -> None:
        self.responses = responses or {}
        self.fail = set(fail or ())
        self.calls: list[dict[str, Any]] = []

    def structured(self, *, label: str, output_model: type[BaseModel], instructions: str, context: str = "",
                   images: list[tuple[str, bytes]] | None = None, effort: str | None = None,
                   max_tokens: int | None = None) -> tuple[BaseModel | None, str | None]:
        self.calls.append({"label": label, "instructions": instructions, "context": context, "images": images,
                           "effort": effort})
        if label in self.fail:
            return None, "simulated outage"
        handler = self.responses.get(label)
        if handler is None:
            return None, "no canned response"
        if callable(handler):
            value = handler(instructions)
        elif isinstance(handler, list):
            value = handler.pop(0) if len(handler) > 1 else handler[0]
        else:
            value = handler
        if isinstance(value, dict):
            value = output_model.model_validate(value)
        return value, None

    def labels(self) -> list[str]:
        return [c["label"] for c in self.calls]


@pytest.fixture(scope="session")
def fixture_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("decks"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None, app_env="test", anthropic_api_key=None, ic_app_token=None, ten_analytics_endpoint=None,
        ten_analytics_api_key=None, resend_api_key=None, upload_dir=tmp_path / "uploads", llm_enabled=False,
        interview_max_questions=6,
        interview_max_followups_per_topic=2,
    )


@pytest.fixture
def service(settings: Settings) -> InterviewService:
    return InterviewService(settings, None)


@pytest.fixture
def session() -> InterviewSession:
    return SessionStore(3600).create()


def upload(service: InterviewService, session: InterviewSession, files: dict[str, Path], *, key: str = "sample_pdf",
           consent: ConsentSettings | None = None, user: UserInfo | None = None,
           inputs: dict[str, str] | None = None, notes: str = "") -> None:
    path = files[key]
    service.start_session(session, filename=path.name, data=path.read_bytes(), content_type=None,
                          user_inputs=inputs or {}, notes=notes, consent=consent or ConsentSettings(),
                          user=user or UserInfo())


def run_interview(service: InterviewService, session: InterviewSession, *,
                  answer: str | Callable[[int], str] = GOOD_ANSWER, limit: int | None = None) -> int:
    """Confirm the profile and answer questions until the interview completes (or ``limit``)."""
    if session.stage == "review":
        service.confirm_profile(session, {})
    count = 0
    while session.interview.current is not None and (limit is None or count < limit):
        text = answer(count) if callable(answer) else answer
        service.submit_answer(session, question_id=session.interview.current.question_id, answer=text)
        count += 1
    return count


@pytest.fixture
def completed_session(service: InterviewService, session: InterviewSession,
                      fixture_files: dict[str, Path]) -> InterviewSession:
    upload(service, session, fixture_files)
    run_interview(service, session)
    service.finish_interview(session)
    return session

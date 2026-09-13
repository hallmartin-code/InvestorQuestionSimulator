"""Server-side interview session state and an in-memory store with hard retention.

Sessions live only in server memory, keyed by a random 256-bit identifier held in an
HttpOnly cookie. A session (deck text, profile, answers, report) is discarded once it is
older than ``DATA_RETENTION_HOURS``, regardless of activity. Uploaded files are never kept:
only the extracted text survives parsing.

The store is per-process. Run a single worker, or replace ``SessionStore`` with a shared
backend (see README, Deployment) before scaling out.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.ingestion.models import DeckDocument
from app.models.analysis import GapAnalysis
from app.models.analytics import AnalyticsResult, ConsentSettings, EmailResult, UserInfo
from app.models.company import CompanyProfile
from app.models.interview import InterviewState
from app.models.scoring import FinalAssessment
from app.utils.security import new_token

Stage = Literal["upload", "review", "interview", "results"]
MAX_NOTICES = 12
#: Memory guard: beyond this many live sessions, the least recently used tenth are evicted.
MAX_SESSIONS = 2000


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class InterviewSession:
    session_id: str
    csrf_token: str
    analytics_id: str
    created_at: datetime
    last_seen: datetime
    authenticated: bool = False
    stage: Stage = "upload"
    consent: ConsentSettings = field(default_factory=ConsentSettings)
    user: UserInfo = field(default_factory=UserInfo)
    deck: DeckDocument | None = None
    founder_notes: str = ""
    user_inputs: dict[str, str] = field(default_factory=dict)
    context: str = ""
    profile: CompanyProfile | None = None
    gaps: GapAnalysis | None = None
    interview: InterviewState | None = None
    last_feedback_id: str | None = None
    last_step_reason: str = ""
    assessment: FinalAssessment | None = None
    report_pdf: bytes | None = None
    analytics: AnalyticsResult | None = None
    email: EmailResult | None = None
    notices: list[str] = field(default_factory=list)
    mode: Literal["ai", "heuristic"] = "heuristic"
    interview_started_at: datetime | None = None
    interview_finished_at: datetime | None = None
    login_failures: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def add_notice(self, text: str | None) -> None:
        if text and text not in self.notices:
            self.notices.append(text)
            del self.notices[:-MAX_NOTICES]

    def pop_notices(self) -> list[str]:
        notices, self.notices = self.notices, []
        return notices

    @property
    def duration_seconds(self) -> int | None:
        if self.interview_started_at is None:
            return None
        end = self.interview_finished_at or _now()
        return max(0, int((end - self.interview_started_at).total_seconds()))


class SessionStore:
    def __init__(self, retention_seconds: float, *, clock: Callable[[], datetime] = _now) -> None:
        self.retention_seconds = retention_seconds
        self._clock = clock
        self._sessions: dict[str, InterviewSession] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    def _expired(self, session: InterviewSession) -> bool:
        return (self._clock() - session.created_at).total_seconds() > self.retention_seconds

    def create(self) -> InterviewSession:
        now = self._clock()
        session = InterviewSession(session_id=new_token(), csrf_token=new_token(), analytics_id=new_token(16),
                                   created_at=now, last_seen=now)
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = sorted(self._sessions.values(), key=lambda s: s.last_seen)[: len(self._sessions) // 10 or 1]
                for stale in oldest:
                    del self._sessions[stale.session_id]
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> InterviewSession | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._expired(session):
                del self._sessions[session_id]
                return None
            session.last_seen = self._clock()
            return session

    def rotate(self, session: InterviewSession) -> InterviewSession:
        """Issue a new identifier and CSRF token (after login, to prevent session fixation)."""
        with self._lock:
            self._sessions.pop(session.session_id, None)
            session.session_id = new_token()
            session.csrf_token = new_token()
            self._sessions[session.session_id] = session
        return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def purge_expired(self) -> int:
        with self._lock:
            expired = [sid for sid, s in self._sessions.items() if self._expired(s)]
            for sid in expired:
                del self._sessions[sid]
        return len(expired)

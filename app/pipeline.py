"""Application service: runs the interview pipeline for one session.

Deck Upload → File Validation → Text/Table Extraction → Company Profile Creation →
Evidence Mapping → Interview Question Planning → Interactive Investor Interview →
Answer Scoring → Coaching → Adaptive Follow-up Questions → Readiness Scoring →
Practice Plan → One-Page PDF Generation → TEN Capital Analytics Record

The web layer is a thin shell around this class; every step is callable (and tested) here.
Each public method holds the session lock, enforces the session stage, and raises only
``AppError`` subclasses with user-facing messages.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.analysis.company_profile import UPLOAD_FIELDS, apply_corrections, build_context, build_profile
from app.analysis.evidence_mapper import build_evidence_map
from app.analysis.gap_analyzer import analyze_gaps
from app.config import Settings
from app.errors import ReportError, SessionError
from app.ingestion.deck_loader import load_deck
from app.ingestion.pdf_parser import render_pages_png
from app.integrations.email_notifier import send_results_email
from app.integrations.llm_client import StructuredLLM
from app.integrations.ten_analytics import build_analytics_record, send_analytics
from app.interview.adaptive_engine import decide_next, follow_up_text, record_resolution
from app.interview.coaching_engine import AllowedFigures, coaching_from_model, heuristic_coaching
from app.interview.question_engine import difficulty_for, generate_question, new_question, plan_interview
from app.interview.readiness_engine import build_final_assessment, calculate_readiness
from app.interview.scoring_engine import evaluate_answer, heuristic_scores, scores_from_values
from app.models.analytics import AnalyticsResult, ConsentSettings, EmailResult, UserInfo
from app.models.interview import Difficulty, InterviewState, Question, ResponseRecord
from app.models.scoring import FinalAssessment
from app.reports.pdf_generator import generate_pdf
from app.reports.report_data import build_report_data, report_filename
from app.session import InterviewSession, Stage
from app.utils.logging import get_logger
from app.utils.text import truncate

LOGGER = get_logger(__name__)

MAX_ANSWER_CHARS = 8000
MAX_NOTES_CHARS = 8000
MAX_VISION_PAGES = 15
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")


def _now() -> datetime:
    return datetime.now(UTC)


class InterviewService:
    def __init__(self, settings: Settings, llm: StructuredLLM | None, *, analytics_client: httpx.Client | None = None,
                 email_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.llm = llm
        self.analytics_client = analytics_client
        self.email_client = email_client

    # --- helpers --------------------------------------------------------------------------

    @property
    def ai_available(self) -> bool:
        return self.llm is not None and self.llm.available

    def llm_for(self, session: InterviewSession) -> StructuredLLM | None:
        """The model client, only when configured and the founder consented to AI processing."""
        if session.consent.ai_processing_consent and self.ai_available:
            return self.llm
        return None

    @staticmethod
    def _require_stage(session: InterviewSession, *stages: Stage) -> None:
        if session.stage not in stages:
            messages = {
                "upload": "Upload a deck to start a new interview.",
                "review": "Review your company profile before starting the interview.",
                "interview": "Your interview is in progress.",
                "results": "This interview is complete. Start a new session to practice again.",
            }
            raise SessionError(messages[session.stage])

    @staticmethod
    def _activate(state: InterviewState, question: Question) -> None:
        state.asked.append(question)
        state.current = question
        if question.category not in state.covered_categories:
            state.covered_categories.append(question.category)

    def _allowed_text(self, session: InterviewSession, extra: str = "") -> str:
        """Everything a coaching figure may legitimately come from."""
        parts = [session.context, session.founder_notes, " ".join(session.user_inputs.values()), extra]
        if session.profile is not None:
            parts.extend(fv.display for fv in session.profile.fields.values())
        if session.interview is not None:
            parts.extend(r.founder_answer for r in session.interview.responses)
            parts.extend(q.text for q in session.interview.asked)
        return "\n".join(p for p in parts if p)

    # --- 1-4: upload, validation, extraction, profile ---------------------------------------------

    def start_session(self, session: InterviewSession, *, filename: str | None, data: bytes,
                      content_type: str | None, user_inputs: dict[str, str], notes: str,
                      consent: ConsentSettings, user: UserInfo) -> None:
        with session.lock:
            self._require_stage(session, "upload")
            if user.email and not _EMAIL.match(user.email):
                raise SessionError("Enter a valid email address, or leave the email field blank.")
            if consent.marketing_consent and not user.email:
                raise SessionError("Enter your email address if you'd like TEN Capital to follow up, "
                                   "or untick the follow-up option.")

            doc = load_deck(filename, data, content_type=content_type, settings=self.settings)

            session.consent = consent
            session.user = UserInfo(name=truncate(user.name or "", 100) or None, email=user.email or None)
            session.founder_notes = truncate(notes or "", MAX_NOTES_CHARS) if notes and notes.strip() else ""
            session.user_inputs = {k: truncate(v, 200) for k, v in user_inputs.items() if k in UPLOAD_FIELDS and v.strip()}
            llm = self.llm_for(session)
            session.mode = "ai" if llm is not None else "heuristic"

            context, context_warnings = build_context(doc, session.founder_notes)
            images = None
            if llm is not None and doc.is_scanned and doc.source_format == "pdf":
                images = render_pages_png(data, doc.low_text_pages, max_pages=MAX_VISION_PAGES)
            profile, warnings, _used = build_profile(
                doc, notes=session.founder_notes, user_inputs=session.user_inputs, context=context, llm=llm,
                effort=self.settings.llm_effort, page_images=images,
            )
            session.deck = doc
            session.context = context
            session.profile = profile
            for warning in [*doc.warnings, *context_warnings, *warnings]:
                session.add_notice(warning)
            if consent.ai_processing_consent and not self.ai_available:
                session.add_notice("AI processing is not configured on this server, so the interview runs in "
                                   "heuristic mode (rule-based extraction, standard questions, heuristic scoring).")
            session.stage = "review"
            LOGGER.info("Session profile built: %d fields, mode=%s", len(profile.fields), session.mode)

    # --- 5-6: review, evidence and gaps, planning, first question -------------------------------------

    def confirm_profile(self, session: InterviewSession, edits: dict[str, str]) -> None:
        with session.lock:
            self._require_stage(session, "review")
            assert session.profile is not None
            for warning in apply_corrections(session.profile, edits):
                session.add_notice(warning)
            llm = self.llm_for(session)
            gaps = analyze_gaps(session.deck, session.profile, context=session.context, llm=llm,
                                effort=self.settings.llm_effort)
            for warning in gaps.warnings:
                session.add_notice(warning)
            state = InterviewState(max_questions=self.settings.interview_max_questions)
            state.plan = plan_interview(session.profile, gaps, state.max_questions)
            session.gaps = gaps
            session.interview = state
            self._ask_next_topic(session, llm)
            session.stage = "interview"
            session.interview_started_at = _now()

    def _ask_next_topic(self, session: InterviewSession, llm: StructuredLLM | None) -> None:
        state = session.interview
        assert state is not None and session.profile is not None and session.gaps is not None
        topic = state.plan.pop(0)
        difficulty = difficulty_for(state.answered, state.max_questions)
        question, notice = generate_question(
            llm=llm, effort=self.settings.llm_question_effort, context=session.context, profile=session.profile,
            gaps=session.gaps, state=state, topic=topic, difficulty=difficulty,
        )
        session.add_notice(notice)
        self._activate(state, question)

    # --- 7-10: answer, scoring, coaching, adaptive follow-up -----------------------------------------------

    def submit_answer(self, session: InterviewSession, *, question_id: str, answer: str) -> ResponseRecord:
        with session.lock:
            self._require_stage(session, "interview")
            state = session.interview
            assert state is not None and session.profile is not None
            question = state.current
            if question is None:
                raise SessionError("The interview is complete. Finish it to see your results.")
            if question_id != question.question_id:
                raise SessionError("That question was already answered. Continue with the current question.")
            answer = (answer or "").strip()
            if not answer:
                raise SessionError("Type an answer before submitting, or finish the interview.")
            if len(answer) > MAX_ANSWER_CHARS:
                answer = answer[:MAX_ANSWER_CHARS]
                session.add_notice(f"Your answer was trimmed to {MAX_ANSWER_CHARS:,} characters.")

            llm = self.llm_for(session)
            evaluation = None
            if llm is not None:
                evaluation, error = evaluate_answer(llm=llm, effort=self.settings.llm_effort, context=session.context,
                                                    profile=session.profile, question=question, answer=answer,
                                                    state=state)
                if evaluation is None:
                    session.add_notice(f"AI scoring was unavailable for this answer ({error}); heuristic scoring was used.")

            if evaluation is not None:
                scores = scores_from_values(evaluation.clarity, evaluation.evidence, evaluation.credibility,
                                            evaluation.completeness)
                coaching = coaching_from_model(evaluation, AllowedFigures.from_text(self._allowed_text(session, answer)))
                source = "llm"
            else:
                scores = heuristic_scores(question.text, answer)
                coaching = heuristic_coaching(question.category, question.text, answer, scores, session.profile)
                source = "heuristic"

            response = ResponseRecord(
                question_id=question.question_id, category=question.category, question=question.text,
                difficulty=question.difficulty, is_follow_up=question.is_follow_up, founder_answer=answer,
                scores=scores, investor_assessment=coaching.investor_assessment, strengths=coaching.strengths,
                weaknesses=coaching.weaknesses, recommended_answer_structure=coaching.answer_structure,
                model_answer=coaching.model_answer, follow_up_required=coaching.follow_up_required,
                follow_up_reason=coaching.follow_up_reason, founder_claims=coaching.founder_claims,
                contradictions=coaching.contradictions, placeholders_inserted=coaching.placeholders_inserted,
                scoring_source=source,  # type: ignore[arg-type]
            )
            state.responses.append(response)
            state.current = None
            record_resolution(state, question, response)
            session.last_feedback_id = question.question_id

            step = decide_next(state, response, max_followups_per_topic=self.settings.interview_max_followups_per_topic)
            session.last_step_reason = step.reason
            asked_next = False
            if step.action == "follow_up":
                text = follow_up_text(response, proposed=coaching.follow_up_question, asked=state.asked)
                if text:
                    proposed = " ".join((coaching.follow_up_question or "").split())
                    difficulty = Difficulty(max(min(int(question.difficulty) + 1, 3),
                                                int(difficulty_for(state.answered, state.max_questions))))
                    follow = new_question(state, category=question.category, text=text, difficulty=difficulty,
                                          source="llm" if text == proposed else "template",
                                          intent=f"Follow-up: {step.reason}", issue_ids=question.issue_ids,
                                          parent=question)
                    state.followups_by_category[question.category.value] = \
                        state.followups_by_category.get(question.category.value, 0) + 1
                    self._activate(state, follow)
                    asked_next = True
            if not asked_next and step.action != "complete" and state.plan and state.answered < state.max_questions:
                self._ask_next_topic(session, llm)
                asked_next = True
            if not asked_next:
                state.completed = True
                session.last_step_reason = step.reason if step.action == "complete" else "All planned topics covered."
            return response

    # --- 11-14: readiness, practice plan, PDF, analytics ------------------------------------------------

    def finish_interview(self, session: InterviewSession) -> FinalAssessment:
        with session.lock:
            if session.stage == "results" and session.assessment is not None:
                return session.assessment
            self._require_stage(session, "interview")
            state = session.interview
            assert state is not None and session.profile is not None
            if state.answered == 0:
                raise SessionError("Answer at least one question before finishing the interview.")
            if state.current is not None:
                state.ended_early = True
                state.current = None
            readiness = calculate_readiness(state.responses, session.gaps, state.resolved_issue_ids)
            assessment, notice = build_final_assessment(
                company=session.profile.company_name, readiness=readiness, responses=state.responses,
                gaps=session.gaps, profile=session.profile, resolved_issue_ids=state.resolved_issue_ids,
                llm=self.llm_for(session), context=session.context, effort=self.settings.llm_effort,
                allowed_text=self._allowed_text(session),
            )
            session.add_notice(notice)
            session.assessment = assessment
            session.interview_finished_at = _now()
            session.stage = "results"
            session.analytics = self.deliver_analytics(session)
            session.email = self.deliver_results_email(session)
            return assessment

    def deliver_results_email(self, session: InterviewSession) -> EmailResult:
        """Email the results (summary + PDF) to the configured TEN Capital inbox. Never raises."""
        try:
            if not self.settings.email_configured:
                return send_results_email(session, self.settings, pdf=None, pdf_name="", export=None,
                                          client=self.email_client)
            pdf = None
            if self.settings.email_attach_pdf:
                try:
                    pdf = self.report_pdf(session)
                except ReportError:
                    LOGGER.warning("Results email will be sent without the PDF: rendering failed")
            export = None
            if self.settings.email_attach_json and session.consent.data_storage_consent:
                export = self.export_session(session)
            return send_results_email(session, self.settings, pdf=pdf, pdf_name=report_filename(session.profile),
                                      export=export, client=self.email_client)
        except Exception:  # noqa: BLE001 - the email must never block the assessment
            LOGGER.exception("Results email could not be prepared")
            return EmailResult(status="failed", detail="The results email could not be prepared.")

    def deliver_analytics(self, session: InterviewSession) -> AnalyticsResult:
        """Build and send the analytics record. Never raises."""
        try:
            record = build_analytics_record(session)
        except Exception:  # noqa: BLE001 - analytics must never block the assessment
            LOGGER.exception("Analytics record could not be built")
            return AnalyticsResult(status="failed", detail="The analytics record could not be built.")
        if record is None:
            return AnalyticsResult(status="skipped", detail="Analytics sharing was not permitted for this session.")
        return send_analytics(record, self.settings, client=self.analytics_client)

    def report_pdf(self, session: InterviewSession) -> bytes:
        with session.lock:
            self._require_stage(session, "results")
            if session.report_pdf is None:
                session.report_pdf = generate_pdf(build_report_data(session))
            return session.report_pdf

    def export_session(self, session: InterviewSession) -> dict[str, Any]:
        """The session's structured data (profile, evidence, interview, scores) for the founder to download."""
        with session.lock:
            state, profile, assessment = session.interview, session.profile, session.assessment
            return {
                "deck": {"filename": session.deck.filename, "slides": session.deck.page_count} if session.deck else None,
                "mode": session.mode,
                "consent": session.consent.model_dump(mode="json"),
                "company_profile": profile.to_structured() if profile else None,
                "evidence_map": build_evidence_map(profile) if profile else [],
                "findings": [i.model_dump(mode="json") for i in session.gaps.issues] if session.gaps else [],
                "interview": {
                    "questions": [q.model_dump(mode="json") for q in state.asked] if state else [],
                    "responses": [r.to_record() for r in state.responses] if state else [],
                    "resolved_issue_ids": state.resolved_issue_ids if state else [],
                    "completed": bool(state and state.completed),
                    "ended_early": bool(state and state.ended_early),
                },
                "assessment": assessment.model_dump(mode="json") if assessment else None,
                "analytics": session.analytics.model_dump(mode="json") if session.analytics else None,
                "results_email": session.email.model_dump(mode="json", exclude={"recipients"}) if session.email else None,
            }

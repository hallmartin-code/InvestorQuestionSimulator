from __future__ import annotations

import pytest

from app.errors import SessionError
from app.interview.question_engine import is_near_duplicate
from app.models.analytics import ConsentSettings, UserInfo
from app.models.interview import Difficulty
from app.pipeline import InterviewService
from tests.conftest import GOOD_ANSWER, WEAK_ANSWER, FakeLLM, run_interview, upload

AI = ConsentSettings(ai_processing_consent=True)

QUESTIONS = [
    "Your deck cites a $6.2B TAM on slide 4. How did you calculate it, and what share is reachable in five years?",
    "At $85K per hospital per year, who signs the contract and how long does procurement take?",
    "You show 180% growth. Over what period, from what base, and what drove it?",
    "Who owns discharge planning budgets inside a community hospital, and how do you reach them?",
    "What does the $4M SAFE buy in milestones, and why is $1.5M committed without a lead?",
    "Why is a $20M cap justified with $1.2M ARR today?",
    "Which incumbent EHR vendor could replicate automated follow-up fastest, and why wouldn't they?",
    "What is the single biggest risk to reaching 40 hospitals in 18 months?",
]
FOLLOW_UP = "What did it cost you in sales time and travel to sign your last three hospitals?"


def ai_llm() -> FakeLLM:
    asked = iter(QUESTIONS)

    def question(_instructions: str) -> dict:
        return {"question": next(asked), "intent": "Tests command of the topic."}

    def evaluate(instructions: str) -> dict:
        weak = f"<founder_answer>\n{WEAK_ANSWER}" in instructions
        return {
            "clarity": 3 if weak else 9, "evidence": 0 if weak else 5, "credibility": 2 if weak else 5,
            "completeness": 1 if weak else 4,
            "investor_assessment": "The founder could not quantify acquisition cost." if weak else "Clear and evidenced.",
            "what_worked": [] if weak else ["Specific ARR figure"],
            "needs_improvement": ["No CAC figure; mentions a $7.7M pipeline without support"] if weak else [],
            "better_answer_structure": "1) Direct answer 2) Evidence 3) Implication",
            "model_answer": "Our CAC is [CAC] and we closed $3.3M in bookings, on $1.2M ARR.",
            "founder_claims": [], "contradictions": [], "follow_up_required": weak,
            "follow_up_reason": "CAC unknown" if weak else "", "follow_up_question": FOLLOW_UP,
        }

    return FakeLLM({
        "extract_profile": {"fields": [
            {"field": "target_customer", "value": "Community hospitals", "page": 1,
             "quote": "AI discharge coordination for community hospitals", "basis": "inferred"}]},
        "gap_analysis": {"issues": []},
        "generate_question": question,
        "evaluate_answer": evaluate,
    })


def test_heuristic_interview_end_to_end(service, session, fixture_files):
    upload(service, session, fixture_files)
    assert session.stage == "review" and session.mode == "heuristic"
    service.confirm_profile(session, {})
    state = session.interview
    assert session.stage == "interview" and state.current.difficulty is Difficulty.UNDERSTANDING
    assert session.gaps.issues and state.plan

    answered = run_interview(service, session)
    assert state.completed and state.current is None
    assert answered == state.answered <= state.max_questions
    texts = [q.text for q in state.asked]
    assert all(not is_near_duplicate(a, b) for i, a in enumerate(texts) for b in texts[i + 1:])
    assert all(r.scoring_source == "heuristic" and r.model_answer for r in state.responses)

    assessment = service.finish_interview(session)
    assert session.stage == "results" and not state.ended_early
    assert assessment.readiness.overall_score is not None and assessment.practice_plan
    assert session.analytics.status == "skipped"
    assert service.finish_interview(session) is assessment


def test_ai_interview_scores_coaches_and_follows_up(settings, session, fixture_files):
    llm = ai_llm()
    service = InterviewService(settings, llm)
    upload(service, session, fixture_files, consent=AI)
    assert session.mode == "ai"
    service.confirm_profile(session, {})
    first = session.interview.current
    assert first.source == "llm" and first.text == QUESTIONS[0]

    weak = service.submit_answer(session, question_id=first.question_id, answer=WEAK_ANSWER)
    assert weak.scoring_source == "llm" and weak.scores.evidence == 0 and weak.follow_up_required
    assert "$7.7M" not in weak.weaknesses[0] and "$3.3M" not in weak.model_answer
    assert "$1.2M ARR" in weak.model_answer and weak.placeholders_inserted == 2
    follow = session.interview.current
    assert follow.is_follow_up and follow.text == FOLLOW_UP and follow.category is first.category
    assert follow.source == "llm" and follow.difficulty >= Difficulty.EVIDENCE

    strong = service.submit_answer(session, question_id=follow.question_id, answer=GOOD_ANSWER)
    assert strong.scores.clarity == 5 and strong.scores.total == 19  # model's 9 clamped to 5
    assert not session.interview.current.is_follow_up

    run_interview(service, session)
    assessment = service.finish_interview(session)
    assert assessment.narrative_source == "heuristic"  # no canned summary: deterministic fallback
    assert any("report narrative was unavailable" in n for n in session.notices)
    contexts = {c["context"] for c in llm.calls}
    assert len(contexts) == 1 and "<deck" in contexts.pop()  # stable, cacheable prefix
    assert llm.labels()[:3] == ["extract_profile", "gap_analysis", "generate_question"]


def test_model_is_never_called_without_ai_consent(settings, session, fixture_files):
    llm = ai_llm()
    service = InterviewService(settings, llm)
    upload(service, session, fixture_files, consent=ConsentSettings(analytics_consent=True))
    run_interview(service, session, limit=2)
    service.finish_interview(session)
    assert llm.calls == [] and session.mode == "heuristic"


def test_model_outage_degrades_to_heuristic_mode(settings, session, fixture_files):
    llm = FakeLLM(fail={"extract_profile", "gap_analysis", "generate_question", "evaluate_answer", "final_summary"})
    service = InterviewService(settings, llm)
    upload(service, session, fixture_files, consent=AI)
    run_interview(service, session)
    assessment = service.finish_interview(session)
    assert session.stage == "results" and assessment.readiness.overall_score is not None
    responses = session.interview.responses
    assert responses and all(r.scoring_source == "heuristic" for r in responses)
    assert all(q.source == "template" for q in session.interview.asked)


def test_answer_validation_and_stage_guards(service, session, fixture_files):
    with pytest.raises(SessionError):
        service.confirm_profile(session, {})
    upload(service, session, fixture_files)
    with pytest.raises(SessionError, match="in progress|Review"):
        upload(service, session, fixture_files)
    service.confirm_profile(session, {})
    current = session.interview.current
    with pytest.raises(SessionError, match="Type an answer"):
        service.submit_answer(session, question_id=current.question_id, answer="   ")
    with pytest.raises(SessionError, match="already answered"):
        service.submit_answer(session, question_id="market_99", answer=GOOD_ANSWER)
    with pytest.raises(SessionError, match="at least one question"):
        service.finish_interview(session)
    with pytest.raises(SessionError):
        service.report_pdf(session)


def test_upload_rejects_invalid_contact_details(service, session, fixture_files):
    with pytest.raises(SessionError, match="valid email"):
        upload(service, session, fixture_files, user=UserInfo(email="not-an-email"))
    with pytest.raises(SessionError, match="email address"):
        upload(service, session, fixture_files, consent=ConsentSettings(analytics_consent=True, marketing_consent=True))
    assert session.stage == "upload"


def test_incomplete_interview_produces_provisional_assessment(service, session, fixture_files):
    upload(service, session, fixture_files)
    run_interview(service, session, limit=2)
    assessment = service.finish_interview(session)
    assert session.interview.ended_early and session.interview.answered == 2
    assert assessment.readiness.provisional
    assert any(c.score is None for c in assessment.readiness.category_scores)
    assert session.stage == "results"


def test_session_export_uses_the_session_data_model(completed_session, service):
    export = service.export_session(completed_session)
    response = export["interview"]["responses"][0]
    assert {"question_id", "category", "question", "founder_answer", "scores", "investor_assessment", "strengths",
            "weaknesses", "recommended_answer_structure", "model_answer", "follow_up_required"} <= set(response)
    assert set(response["scores"]) >= {"clarity", "evidence", "credibility", "completeness", "total"}
    assert export["company_profile"]["fundraising"]["raise_amount"] == 4_000_000
    assert any(e["status"] == "MISSING" for e in export["evidence_map"])
    assert export["assessment"]["readiness"]["overall_score"] is not None

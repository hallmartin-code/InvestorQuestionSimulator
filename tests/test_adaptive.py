from __future__ import annotations

from app.interview.adaptive_engine import (
    CATEGORY_FOLLOW_UPS,
    decide_next,
    follow_up_text,
    needs_follow_up,
    record_resolution,
)
from app.models.interview import Category, Difficulty, InterviewState, Question, ResponseRecord, TopicPlan
from app.models.scoring import DimensionScores

SALES_PROCESS_FOLLOW_UP = CATEGORY_FOLLOW_UPS[Category.UNIT_ECONOMICS][0]


def make_state(*, plan=(Category.TEAM,), max_questions=10) -> InterviewState:
    return InterviewState(plan=[TopicPlan(category=c, priority=1) for c in plan], max_questions=max_questions)


def ask(state: InterviewState, category: Category, text: str, *, follow_up: bool = False, issue_ids=None) -> Question:
    question = Question(question_id=f"{category.value}_{len(state.asked) + 1:02d}", category=category, text=text,
                        difficulty=Difficulty.EVIDENCE, is_follow_up=follow_up, issue_ids=issue_ids or [])
    state.asked.append(question)
    return question


def answer(state: InterviewState, question: Question, scores: tuple[int, int, int, int], **extra) -> ResponseRecord:
    clarity, evidence, credibility, completeness = scores
    record = ResponseRecord(question_id=question.question_id, category=question.category, question=question.text,
                            difficulty=question.difficulty, founder_answer="...",
                            scores=DimensionScores(clarity=clarity, evidence=evidence, credibility=credibility,
                                                   completeness=completeness), **extra)
    state.responses.append(record)
    return record


def test_missing_cac_triggers_the_sales_process_follow_up():
    state = make_state()
    q = ask(state, Category.UNIT_ECONOMICS, "What is your customer acquisition cost?")
    r = answer(state, q, (3, 0, 2, 0))
    need, reasons = needs_follow_up(r)
    assert need and "weak evidence" in reasons
    step = decide_next(state, r, max_followups_per_topic=2)
    assert step.action == "follow_up"
    assert follow_up_text(r, proposed=None, asked=state.asked) == SALES_PROCESS_FOLLOW_UP


def test_adequate_answer_moves_to_next_topic():
    state = make_state(plan=(Category.TEAM, Category.RISKS))
    q = ask(state, Category.MARKET, "How big is the market?")
    step = decide_next(state, answer(state, q, (4, 4, 4, 4)), max_followups_per_topic=2)
    assert step.action == "next_topic" and step.topic.category is Category.TEAM


def test_evaluator_flag_triggers_follow_up_only_below_ceiling():
    state = make_state()
    q = ask(state, Category.TRACTION, "Break down your ARR.")
    flagged = answer(state, q, (4, 3, 4, 3), follow_up_required=True, follow_up_reason="Growth period unclear")
    assert decide_next(state, flagged, max_followups_per_topic=2).action == "follow_up"
    strong = answer(state, q, (5, 4, 4, 4), follow_up_required=True)
    assert decide_next(state, strong, max_followups_per_topic=2).action == "next_topic"


def test_follow_ups_are_capped_per_topic():
    state = make_state()
    q = ask(state, Category.MARKET, "How did you size the market?")
    state.followups_by_category["market"] = 2
    step = decide_next(state, answer(state, q, (3, 1, 3, 3)), max_followups_per_topic=2)
    assert step.action == "next_topic" and "limit" in step.reason


def test_moves_on_when_founder_cannot_answer_after_a_follow_up():
    state = make_state()
    ask(state, Category.MARKET, "How did you size the market?")
    follow = ask(state, Category.MARKET, "How did you calculate that?", follow_up=True)
    step = decide_next(state, answer(state, follow, (1, 0, 1, 1)), max_followups_per_topic=3)
    assert step.action == "next_topic" and "could not resolve" in step.reason


def test_budget_and_plan_exhaustion_complete_the_interview():
    state = make_state(max_questions=1)
    q = ask(state, Category.MARKET, "Market?")
    assert decide_next(state, answer(state, q, (1, 0, 1, 0)), max_followups_per_topic=2).action == "complete"

    state = make_state(plan=(), max_questions=5)
    q = ask(state, Category.MARKET, "Market?")
    assert decide_next(state, answer(state, q, (4, 4, 4, 4)), max_followups_per_topic=2).action == "complete"


def test_last_slot_goes_to_an_uncovered_topic():
    state = make_state(plan=(Category.FUNDRAISING,), max_questions=2)
    q = ask(state, Category.MARKET, "Market?")
    step = decide_next(state, answer(state, q, (2, 1, 2, 1)), max_followups_per_topic=2)
    assert step.action == "next_topic" and step.topic.category is Category.FUNDRAISING


def test_follow_up_text_prefers_new_model_proposal_and_avoids_repeats():
    state = make_state()
    q = ask(state, Category.UNIT_ECONOMICS, "What is your customer acquisition cost?")
    r = answer(state, q, (3, 0, 2, 0))
    proposed = "You mentioned paid pilots. What did the last three pilots cost you to win, in total?"
    assert follow_up_text(r, proposed=proposed, asked=state.asked) == proposed
    ask(state, Category.UNIT_ECONOMICS, SALES_PROCESS_FOLLOW_UP, follow_up=True)
    repeat = follow_up_text(r, proposed="What is your customer acquisition cost?", asked=state.asked)
    assert repeat not in (None, SALES_PROCESS_FOLLOW_UP, "What is your customer acquisition cost?")


def test_strong_answers_resolve_probed_findings():
    state = make_state()
    q = ask(state, Category.MARKET, "Source your TAM.", issue_ids=["R03"])
    record_resolution(state, q, answer(state, q, (3, 3, 3, 3)))
    assert state.resolved_issue_ids == []
    record_resolution(state, q, answer(state, q, (4, 4, 3, 3)))
    assert state.resolved_issue_ids == ["R03"]

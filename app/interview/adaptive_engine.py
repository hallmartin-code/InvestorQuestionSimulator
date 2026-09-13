"""Adaptive interview logic: whether to press on a weak answer or move to the next topic.

Decisions are deterministic so the interview is predictable and testable:

* A follow-up is asked when the answer is weak on evidence, credibility or completeness
  (<= 2 of 5), or when the evaluator flags an unresolved issue on an answer below 16/20.
* Follow-ups are capped per topic, and the interview moves on when a founder still cannot
  answer after one follow-up (the gap is recorded for the practice plan instead).
* A follow-up is not taken from the last question slot while planned topics remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.interview.question_engine import is_duplicate
from app.models.interview import Category, InterviewState, Question, ResponseRecord, TopicPlan

C = Category

WEAK_DIMENSION = 2
EVALUATOR_FLAG_CEILING = 16
MOVE_ON_SCORE = 6
RESOLVED_SCORE = 14

CATEGORY_FOLLOW_UPS: dict[Category, tuple[str, ...]] = {
    C.UNIT_ECONOMICS: ("Walk me through your current sales process. How many people are involved, how long does a typical "
                       "sale take, and what does it cost you to acquire a customer?",
                       "How many months does it take to recover what you spend acquiring a customer?"),
    C.MARKET: ("How did you calculate that, and which source supports each input?",),
    C.TRACTION: ("How many of those customers are paying today, and what percentage renew?",),
    C.CUSTOMER: ("Who specifically is buying, and what percentage of the prospects you talk to behave that way?",),
    C.COMPETITION: ("What prevents a competitor from copying this?",),
    C.DEFENSIBILITY: ("What prevents a competitor from copying this?",),
    C.FUNDRAISING: ("Why does this require venture capital, and what milestone does this financing actually purchase?",),
    C.VALUATION: ("Why is this valuation justified today?",),
    C.USE_OF_FUNDS: ("What milestone does this financing actually purchase?",),
    C.FINANCIAL_PROJECTIONS: ("What happens to the plan if that assumption is wrong?",),
    C.GO_TO_MARKET: ("What does it cost you to acquire one customer, and how many months does it take to recover that cost?",),
    C.BUSINESS_MODEL: ("What evidence supports that assumption?",),
}
DIMENSION_FOLLOW_UPS: dict[str, str] = {
    "evidence": "What evidence supports that? Give me the actual numbers and where they come from.",
    "credibility": "What happens if that assumption is wrong, and what makes you confident it isn't?",
    "completeness": "Let me press on the part you didn't cover. Which piece of my question is hardest to answer today, "
                    "and what would you need to answer it fully?",
    "clarity": "Give me the short version: in two sentences, what is the direct answer?",
}


@dataclass(frozen=True)
class NextStep:
    action: Literal["follow_up", "next_topic", "complete"]
    reason: str
    topic: TopicPlan | None = None


def needs_follow_up(response: ResponseRecord) -> tuple[bool, list[str]]:
    s = response.scores
    reasons: list[str] = []
    if s.evidence <= WEAK_DIMENSION:
        reasons.append("weak evidence")
    if s.credibility <= WEAK_DIMENSION:
        reasons.append("credibility concerns")
    if s.completeness <= WEAK_DIMENSION:
        reasons.append("incomplete answer")
    if response.contradictions:
        reasons.append("contradiction")
    if response.follow_up_required and s.total < EVALUATOR_FLAG_CEILING and not reasons:
        reasons.append(response.follow_up_reason or "unresolved issue")
    return bool(reasons), reasons


def follow_up_chain_length(state: InterviewState, category: Category) -> int:
    """Follow-ups asked in a row on ``category`` at the end of the interview so far."""
    count = 0
    for question in reversed(state.asked):
        if question.category is not category or not question.is_follow_up:
            break
        count += 1
    return count


def decide_next(state: InterviewState, response: ResponseRecord, *, max_followups_per_topic: int) -> NextStep:
    if state.answered >= state.max_questions:
        return NextStep("complete", "Question budget reached.")
    need, reasons = needs_follow_up(response)
    if need:
        chain = follow_up_chain_length(state, response.category)
        used = state.followups_by_category.get(response.category.value, 0)
        remaining = state.max_questions - state.answered
        if chain >= 1 and response.scores.total <= MOVE_ON_SCORE:
            reason = "The founder could not resolve this after a follow-up; moving on and flagging it for practice."
        elif used >= max_followups_per_topic:
            reason = "Follow-up limit for this topic reached; moving on."
        elif remaining <= 1 and state.plan:
            reason = "Saving the last question for an uncovered topic."
        else:
            return NextStep("follow_up", "Following up: " + ", ".join(reasons) + ".")
        if state.plan:
            return NextStep("next_topic", reason, topic=state.plan[0])
        return NextStep("complete", reason)
    if state.plan:
        return NextStep("next_topic", "Answer was adequate; moving to the next topic.", topic=state.plan[0])
    return NextStep("complete", "All planned topics covered.")


def follow_up_text(response: ResponseRecord, *, proposed: str | None, asked: list[Question]) -> str | None:
    """The follow-up to ask: the evaluator's proposal when it is new, else a targeted template."""
    if proposed and len(proposed.split()) >= 4 and not is_duplicate(proposed, asked):
        return " ".join(proposed.split())
    for text in CATEGORY_FOLLOW_UPS.get(response.category, ()):
        if not is_duplicate(text, asked):
            return text
    for dimension in (response.scores.weakest_dimension(), "evidence", "credibility", "completeness", "clarity"):
        text = DIMENSION_FOLLOW_UPS[dimension]
        if not is_duplicate(text, asked):
            return text
    return None


def record_resolution(state: InterviewState, question: Question, response: ResponseRecord) -> None:
    """A strong answer resolves the findings the question was probing."""
    if response.scores.total >= RESOLVED_SCORE:
        for issue_id in question.issue_ids:
            if issue_id not in state.resolved_issue_ids:
                state.resolved_issue_ids.append(issue_id)

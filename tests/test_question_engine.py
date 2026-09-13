from __future__ import annotations

from app.interview.question_engine import (
    difficulty_for,
    generate_question,
    is_near_duplicate,
    new_question,
    plan_interview,
    template_question,
    topic_priority,
)
from app.models.analysis import GapAnalysis, Issue, IssueTag
from app.models.company import CompanyProfile, DataStatus, FieldValue
from app.models.interview import CATEGORY_PHASE, Category, Difficulty, InterviewState, TopicPlan


def profile_with(**values) -> CompanyProfile:
    profile = CompanyProfile()
    for name, (value, page) in values.items():
        profile.set(FieldValue(field=name, value=value, status=DataStatus.VERIFIED_FROM_DECK, page_or_slide=page,
                               confidence=0.6))
    return profile


def test_plan_includes_fundraising_and_follows_meeting_order():
    plan = plan_interview(CompanyProfile(), GapAnalysis(), 12)
    categories = [t.category for t in plan]
    assert Category.FUNDRAISING in categories
    assert len(categories) == len(set(categories)) == 9
    phases = [CATEGORY_PHASE[c] for c in categories]
    assert phases == sorted(phases)


def test_red_flags_raise_topic_priority():
    red_flag = Issue(issue_id="R01", tag=IssueTag.RED_FLAG, category=Category.COMPETITION, title="No competition",
                     severity=3)
    base, _ = topic_priority(Category.COMPETITION, "seed", [], 0)
    flagged, reason = topic_priority(Category.COMPETITION, "seed", [red_flag], 0)
    assert flagged > base and reason.startswith("RED_FLAG")
    plan = plan_interview(CompanyProfile(), GapAnalysis(issues=[red_flag]), 6)
    assert Category.COMPETITION in [t.category for t in plan]


def test_difficulty_progression():
    levels = [difficulty_for(i, 12) for i in range(12)]
    assert levels[0] is Difficulty.UNDERSTANDING
    assert levels[5] is Difficulty.EVIDENCE
    assert levels[11] is Difficulty.INVESTMENT_COMMITTEE
    assert levels == sorted(levels)


def test_near_duplicate_detection():
    a = "What does it cost you to acquire a customer, and how do you calculate that?"
    b = "How do you calculate what it costs you to acquire a customer?"
    c = "Who are the logical acquirers for this company, and why?"
    assert is_near_duplicate(a, b)
    assert not is_near_duplicate(a, c)


def test_templates_are_grounded_in_deck_figures():
    profile = profile_with(tam=(6.2e9, 4), som=(1.2e8, 4))
    text = template_question(Category.MARKET, Difficulty.UNDERSTANDING, profile, [])
    assert "$6.2B" in text and "slide 4" in text
    evidence = template_question(Category.MARKET, Difficulty.EVIDENCE, profile, [])
    assert "$6.2B" in evidence and "$120M" in evidence


def test_templates_never_cite_inferred_values():
    profile = CompanyProfile()
    profile.set(FieldValue(field="valuation", value=2e7, status=DataStatus.INFERRED, confidence=0.4))
    assert "$20M" not in template_question(Category.VALUATION, Difficulty.UNDERSTANDING, profile, [])


def test_question_ids_are_unique_per_category():
    state = InterviewState()
    q1 = new_question(state, category=Category.MARKET, text="Q one about market?", difficulty=Difficulty.UNDERSTANDING, source="template")
    state.asked.append(q1)
    q2 = new_question(state, category=Category.MARKET, text="Q two about market?", difficulty=Difficulty.EVIDENCE, source="template", parent=q1)
    assert (q1.question_id, q2.question_id) == ("market_01", "market_02")
    assert q2.is_follow_up and q2.parent_question_id == "market_01"


class _DuplicateLLM:
    available = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def structured(self, *, output_model, **_kwargs):
        self.calls += 1
        return output_model(question=self.text, intent="test"), None


def test_duplicate_model_questions_fall_back_to_templates():
    state = InterviewState()
    asked = new_question(state, category=Category.TRACTION, text="What traction can you point to today, and what numbers support it?",
                         difficulty=Difficulty.UNDERSTANDING, source="template")
    state.asked.append(asked)
    llm = _DuplicateLLM("What traction can you point to today and which numbers support it?")
    question, notice = generate_question(llm=llm, effort=None, context="", profile=CompanyProfile(), gaps=GapAnalysis(),
                                         state=state, topic=TopicPlan(category=Category.TRACTION, priority=3),
                                         difficulty=Difficulty.UNDERSTANDING)
    assert llm.calls == 2
    assert question.source == "template" and notice is None
    assert not is_near_duplicate(question.text, asked.text)


def test_model_question_is_used_when_new_and_failure_is_reported():
    state = InterviewState()
    llm = _DuplicateLLM("Your deck shows $1.2M ARR on slide 5. How much of that is recurring software revenue?")
    question, _ = generate_question(llm=llm, effort=None, context="", profile=CompanyProfile(), gaps=GapAnalysis(),
                                    state=state, topic=TopicPlan(category=Category.TRACTION, priority=3, issue_ids=["R02"]),
                                    difficulty=Difficulty.EVIDENCE)
    assert question.source == "llm" and question.issue_ids == ["R02"] and question.text.startswith("Your deck shows")

    class Failing:
        available = True

        def structured(self, **_kwargs):
            return None, "simulated outage"

    question, notice = generate_question(llm=Failing(), effort=None, context="", profile=CompanyProfile(),
                                         gaps=GapAnalysis(), state=state,
                                         topic=TopicPlan(category=Category.TEAM, priority=2),
                                         difficulty=Difficulty.UNDERSTANDING)
    assert question.source == "template" and "unavailable" in notice

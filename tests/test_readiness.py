from __future__ import annotations

import pytest

from app.interview.readiness_engine import build_final_assessment, build_practice_plan, calculate_readiness
from app.models.analysis import GapAnalysis, Issue, IssueTag
from app.models.company import CompanyProfile
from app.models.interview import Category, Difficulty, ResponseRecord
from app.models.scoring import READINESS_CATEGORIES, DimensionScores, ReadinessLevel, classify_readiness
from tests.conftest import FakeLLM


def response(category: Category, total: int, *, credibility: int | None = None, qid: str | None = None,
             answer: str = "Answer.") -> ResponseRecord:
    base = [total // 4 + (1 if i < total % 4 else 0) for i in range(4)]
    clarity, evidence, cred, completeness = base
    if credibility is not None:
        cred = credibility
    return ResponseRecord(
        question_id=qid or f"{category.value}_01", category=category, question=f"Question about {category.label}?",
        difficulty=Difficulty.EVIDENCE, founder_answer=answer,
        scores=DimensionScores(clarity=clarity, evidence=evidence, credibility=cred, completeness=completeness),
        weaknesses=[f"Weak point on {category.label}"], strengths=[f"Strong point on {category.label}"],
    )


def one_per_readiness_category(total: int = 16, overrides: dict[str, int] | None = None) -> list[ResponseRecord]:
    overrides = overrides or {}
    return [response(Category(rc.categories[0]), overrides.get(rc.key, total)) for rc in READINESS_CATEGORIES]


def test_weights_sum_to_100_and_every_topic_is_mapped_once():
    assert sum(rc.weight for rc in READINESS_CATEGORIES) == 100
    mapped = [value for rc in READINESS_CATEGORIES for value in rc.categories]
    assert sorted(mapped) == sorted(c.value for c in Category)


@pytest.mark.parametrize(("score", "level"), [
    (100, ReadinessLevel.INVESTOR_READY), (90, ReadinessLevel.INVESTOR_READY), (89, ReadinessLevel.STRONG),
    (80, ReadinessLevel.STRONG), (79, ReadinessLevel.PROMISING), (70, ReadinessLevel.PROMISING),
    (69, ReadinessLevel.SIGNIFICANT_PREPARATION), (60, ReadinessLevel.SIGNIFICANT_PREPARATION),
    (59, ReadinessLevel.NOT_READY), (0, ReadinessLevel.NOT_READY),
])
def test_readiness_levels(score, level):
    assert classify_readiness(score) is level


def test_weighted_score_with_full_coverage():
    # Every category 80% except traction (weight 15) at 60%: (85 * 80 + 15 * 60) / 100 = 77.
    result = calculate_readiness(one_per_readiness_category(16, {"traction": 12}))
    assert result.overall_score == 77
    assert result.level is ReadinessLevel.PROMISING
    assert result.coverage_pct == 100 and not result.provisional
    assert result.weakest_category == "Traction"
    assert result.average_answer_score == pytest.approx((16 * 10 + 12) / 11, abs=0.05)


def test_uncovered_categories_are_excluded_and_score_is_provisional():
    # Traction (15) at 60% and Team (10) at 80%: (15 * 60 + 10 * 80) / 25 = 68.
    result = calculate_readiness([response(Category.TRACTION, 12), response(Category.TEAM, 16)])
    assert result.overall_score == 68
    assert result.coverage_pct == 25 and result.provisional
    unassessed = [c for c in result.category_scores if c.score is None]
    assert len(unassessed) == 9


def test_category_score_is_mean_of_its_topics():
    result = calculate_readiness([response(Category.BUSINESS_MODEL, 20), response(Category.UNIT_ECONOMICS, 10)])
    economics = next(c for c in result.category_scores if c.key == "business_model_economics")
    assert economics.score == 75.0 and economics.questions == 2


def test_no_answers_means_no_score():
    result = calculate_readiness([])
    assert result.overall_score is None and result.level is None and result.provisional


def test_arguments_and_highest_risk_topic():
    responses = [response(Category.TRACTION, 18, credibility=5, qid="t1", answer="We have 14 paying hospitals."),
                 response(Category.MARKET, 8, credibility=1, qid="m1", answer="The market is huge."),
                 response(Category.TEAM, 14, qid="team1")]
    gaps = GapAnalysis(issues=[Issue(issue_id="R01", tag=IssueTag.RED_FLAG, category=Category.COMPETITION,
                                     title="Claims no competition", severity=3)])
    result = calculate_readiness(responses, gaps, [])
    assert result.most_credible_argument.question_id == "t1"
    assert result.weakest_argument.question_id == "m1"
    assert result.highest_risk_topic == "Competition"  # unanswered red flag outweighs a weak answer
    resolved = calculate_readiness(responses, gaps, ["R01"])
    assert resolved.highest_risk_topic == "Market"


def test_practice_plan_targets_weakest_categories():
    responses = one_per_readiness_category(18, {"traction": 8, "go_to_market": 10, "team": 12})
    readiness = calculate_readiness(responses)
    plan = build_practice_plan(readiness, responses, None, CompanyProfile())
    assert [p.category for p in plan[:3]] == ["Traction", "Go-to-Market", "Team"]
    assert 3 <= len(plan) <= 5
    for priority in plan:
        assert priority.weakness and priority.why_investors_care and priority.recommended_action
        assert priority.practice_question and priority.missing_evidence
    assert all(p.score < 80 for p in plan)


def test_final_assessment_deterministic_and_model_narrative_is_guarded():
    responses = one_per_readiness_category(16, {"traction": 10})
    readiness = calculate_readiness(responses)
    assessment, notice = build_final_assessment(
        company="CareLoop Health", readiness=readiness, responses=responses, gaps=None, profile=CompanyProfile(),
        resolved_issue_ids=[], llm=None, context="", effort=None, allowed_text="")
    assert notice is None and assessment.narrative_source == "heuristic"
    assert f"{readiness.overall_score}/100" in assessment.investor_assessment
    assert len(assessment.next_steps) == 3 and len(assessment.questions_to_practice) == 3
    assert len(assessment.strongest_areas) == 3 and len(assessment.weakest_areas) == 3
    assert not {a.label for a in assessment.strongest_areas} & {a.label for a in assessment.weakest_areas}

    llm = FakeLLM({"final_summary": {
        "investor_assessment": "CareLoop has $1.2M ARR but claims a $9.9M pipeline that is not evidenced.",
        "investor_concerns": ["Traction depth", "Unit economics unproven", "Market size unsourced"],
        "next_steps": ["Build a metrics sheet", "Calculate CAC payback", "Source the TAM"],
        "practice_plan": [{"category_key": "traction", "weakness": "w", "why_investors_care": "y",
                           "missing_evidence": "m", "recommended_action": "a",
                           "practice_question": "How did ARR grow from [prior ARR]?"}],
    }})
    assessment, notice = build_final_assessment(
        company="CareLoop Health", readiness=readiness, responses=responses, gaps=None, profile=CompanyProfile(),
        resolved_issue_ids=[], llm=llm, context="", effort=None, allowed_text="$1.2M ARR as of June 2025")
    assert assessment.narrative_source == "llm"
    assert "$1.2M" in assessment.investor_assessment and "$9.9M" not in assessment.investor_assessment
    assert "[amount]" in assessment.investor_assessment
    assert assessment.next_steps == ["Build a metrics sheet", "Calculate CAC payback", "Source the TAM"]
    traction = next(p for p in assessment.practice_plan if p.category_key == "traction")
    assert traction.practice_question == "How did ARR grow from [prior ARR]?"

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.interview.scoring_engine import (
    clamp_dimension,
    heuristic_scores,
    response_percentage,
    response_score,
    scores_from_values,
)
from app.models.scoring import DimensionScores
from tests.conftest import GOOD_ANSWER, WEAK_ANSWER


def test_total_and_percentage():
    scores = DimensionScores(clarity=4, evidence=3, credibility=4, completeness=3)
    assert scores.total == 14 == response_score(scores)
    assert scores.percentage == 70.0 == response_percentage(scores)
    assert scores.model_dump() == {"clarity": 4, "evidence": 3, "credibility": 4, "completeness": 3,
                                   "total": 14, "percentage": 70.0}


def test_bounds_and_perfect_score():
    assert DimensionScores(clarity=5, evidence=5, credibility=5, completeness=5).percentage == 100.0
    assert DimensionScores(clarity=0, evidence=0, credibility=0, completeness=0).total == 0
    with pytest.raises(ValidationError):
        DimensionScores(clarity=6, evidence=0, credibility=0, completeness=0)


@pytest.mark.parametrize(("raw", "expected"), [(7, 5), (-2, 0), ("3", 3), (2.6, 3), (None, 0), ("high", 0)])
def test_model_scores_are_clamped(raw, expected):
    assert clamp_dimension(raw) == expected


def test_scores_from_values_clamps_every_dimension():
    scores = scores_from_values(9, -1, "4", 2.4)
    assert (scores.clarity, scores.evidence, scores.credibility, scores.completeness) == (5, 0, 4, 2)


def test_heuristic_empty_and_non_answers():
    assert heuristic_scores("What is your CAC?", "   ").total == 0
    non_answer = heuristic_scores("What is your CAC?", "I don't know")
    assert non_answer.total <= 2


def test_heuristic_rewards_evidence_and_penalizes_hedging():
    question = "Break down your traction: how many customers, what revenue, and how has it trended?"
    strong = heuristic_scores(question, GOOD_ANSWER)
    vague = heuristic_scores(question, "I think we're doing pretty well and maybe growing, customers seem to like it.")
    weak = heuristic_scores("What is your customer acquisition cost?", WEAK_ANSWER)
    assert strong.total > vague.total > 0
    assert strong.evidence >= 4
    assert weak.evidence <= 2 and weak.completeness <= 2


def test_heuristic_penalizes_absolute_claims():
    question = "Who are your competitors?"
    measured = heuristic_scores(question, "Our main competitors are Epic and Cerner, and we win on 30-day follow-up.")
    absolute = heuristic_scores(question, "We have no competition at all, our competitors don't exist.")
    assert absolute.credibility < measured.credibility

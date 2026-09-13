from __future__ import annotations

from app.interview.coaching_engine import AllowedFigures, coaching_from_model, fill_template, guard_figures, heuristic_coaching
from app.interview.scoring_engine import EvaluationOutput
from app.models.company import CompanyProfile, DataStatus, FieldValue
from app.models.interview import Category
from app.models.scoring import DimensionScores

SOURCE = "Traction: $1.2M ARR as of June 2025. 14 paying hospitals. Net revenue retention 112%. TAM $6.2B."


def guard(text: str, source: str = SOURCE) -> tuple[str, int]:
    return guard_figures(text, AllowedFigures.from_text(source))


def test_fabricated_figures_become_placeholders():
    text, count = guard("We have $9.9M in pipeline, 45% conversion and a 3.2x LTV:CAC.")
    assert text == "We have [amount] in pipeline, [percentage] conversion and a [multiple] LTV:CAC."
    assert count == 3


def test_figures_from_the_source_are_kept_including_equivalent_forms():
    text, count = guard("We reached $1.2M ARR ($1,200,000) with 14 hospitals and 112% NRR in a $6.2 billion market.")
    assert count == 0
    assert "$1,200,000" in text and "112%" in text and "$6.2 billion" in text


def test_structural_numbers_and_codes_are_untouched():
    text, count = guard("The three drivers: 1) B2B sales, 2) Q4 renewals, 3) 2 new hires in a 30-day COVID-19 window.")
    assert "B2B" in text and "Q4" in text and "COVID-19" in text
    assert "[figure]-day" in text and count == 1


def test_years_not_in_source_are_replaced():
    text, _ = guard("We will be profitable by 2031, as planned in 2025.")
    assert "[year]" in text and "2025" in text


def test_model_coaching_is_guarded_in_every_field():
    evaluation = EvaluationOutput(
        clarity=4, evidence=2, credibility=3, completeness=3,
        investor_assessment="Claims $1.2M ARR but cites a $5M pipeline without support.",
        what_worked=["Led with ARR of $1.2M"], needs_improvement=["No CAC; 60% margin claim unsupported"],
        better_answer_structure="1) Answer 2) Evidence", model_answer="We have $1.2M ARR and [customer count] customers, growing 300% a year.",
        founder_claims=["$1.2M ARR"], contradictions=[], follow_up_required=True,
        follow_up_reason="CAC unknown", follow_up_question="What does it cost you to acquire a hospital?",
    )
    coaching = coaching_from_model(evaluation, AllowedFigures.from_text(SOURCE))
    assert "$5M" not in coaching.investor_assessment and "[amount]" in coaching.investor_assessment
    assert "60%" not in coaching.weaknesses[0]
    assert "300%" not in coaching.model_answer and "[customer count]" in coaching.model_answer
    assert coaching.placeholders_inserted == 3
    assert coaching.follow_up_required and coaching.follow_up_question


def test_heuristic_model_answer_uses_confirmed_values_and_placeholders_only():
    profile = CompanyProfile()
    profile.set(FieldValue(field="arr", value=1.2e6, status=DataStatus.VERIFIED_FROM_DECK, page_or_slide=5))
    profile.set(FieldValue(field="revenue_growth", value=180.0, status=DataStatus.INFERRED))
    coaching = heuristic_coaching(Category.TRACTION, "Break down your traction.", "We are growing.",
                                  DimensionScores(clarity=2, evidence=1, credibility=2, completeness=1), profile)
    assert "$1.2M" in coaching.model_answer
    assert "180%" not in coaching.model_answer and "[growth rate]" in coaching.model_answer
    assert "[customer count]" in coaching.model_answer
    assert coaching.weaknesses and not coaching.strengths
    assert guard_figures(coaching.model_answer, AllowedFigures.from_text("$1.2M"))[1] == 0


def test_every_category_has_a_model_answer_template():
    profile = CompanyProfile()
    for category in Category:
        text = fill_template(heuristic_coaching(category, "q", "a", DimensionScores(clarity=3, evidence=3, credibility=3,
                                                                                    completeness=3), profile).model_answer,
                             profile)
        assert "[" in text and "{" not in text

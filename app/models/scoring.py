"""Scoring models: per-answer dimensions, readiness categories, levels and the final assessment.

All arithmetic on these models is deterministic Python; the model only proposes the four
0-5 dimension scores for an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, computed_field

if TYPE_CHECKING:  # pragma: no cover
    from app.models.interview import Category

MAX_DIMENSION = 5
MAX_RESPONSE_SCORE = 20


class DimensionScores(BaseModel):
    clarity: int = Field(ge=0, le=MAX_DIMENSION)
    evidence: int = Field(ge=0, le=MAX_DIMENSION)
    credibility: int = Field(ge=0, le=MAX_DIMENSION)
    completeness: int = Field(ge=0, le=MAX_DIMENSION)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.clarity + self.evidence + self.credibility + self.completeness

    @computed_field  # type: ignore[prop-decorator]
    @property
    def percentage(self) -> float:
        return round(self.total / MAX_RESPONSE_SCORE * 100, 1)

    def weakest_dimension(self) -> str:
        values = {"evidence": self.evidence, "credibility": self.credibility,
                  "completeness": self.completeness, "clarity": self.clarity}
        return min(values, key=lambda k: values[k])


@dataclass(frozen=True)
class ReadinessCategory:
    key: str
    label: str
    weight: int
    categories: tuple[str, ...]  # Category values


READINESS_CATEGORIES: tuple[ReadinessCategory, ...] = (
    ReadinessCategory("market_understanding", "Market Understanding", 10, ("market",)),
    ReadinessCategory("problem_customer", "Problem & Customer", 10, ("problem", "customer")),
    ReadinessCategory("product_differentiation", "Product & Differentiation", 10, ("product_solution", "defensibility")),
    ReadinessCategory("traction", "Traction", 15, ("traction",)),
    ReadinessCategory("business_model_economics", "Business Model & Economics", 15, ("business_model", "unit_economics")),
    ReadinessCategory("go_to_market", "Go-to-Market", 10, ("go_to_market",)),
    ReadinessCategory("competitive_position", "Competitive Position", 5, ("competition",)),
    ReadinessCategory("team", "Team", 10, ("team",)),
    ReadinessCategory("financial_command", "Financial Command", 5, ("financial_performance", "financial_projections")),
    ReadinessCategory("fundraising_strategy", "Fundraising Strategy", 5,
                      ("fundraising", "valuation", "use_of_funds", "milestones", "exit_potential")),
    ReadinessCategory("risk_awareness", "Risk Awareness", 5, ("risks",)),
)

READINESS_BY_KEY = {rc.key: rc for rc in READINESS_CATEGORIES}


def readiness_category_for(category: Category | str) -> ReadinessCategory:
    value = getattr(category, "value", category)
    return next(rc for rc in READINESS_CATEGORIES if value in rc.categories)


class ReadinessLevel(str, Enum):
    INVESTOR_READY = "INVESTOR READY"
    STRONG = "STRONG — MINOR PREPARATION NEEDED"
    PROMISING = "PROMISING — SEVERAL WEAK AREAS"
    SIGNIFICANT_PREPARATION = "SIGNIFICANT PREPARATION NEEDED"
    NOT_READY = "NOT YET READY FOR INVESTOR OUTREACH"


def classify_readiness(score: int) -> ReadinessLevel:
    if score >= 90:
        return ReadinessLevel.INVESTOR_READY
    if score >= 80:
        return ReadinessLevel.STRONG
    if score >= 70:
        return ReadinessLevel.PROMISING
    if score >= 60:
        return ReadinessLevel.SIGNIFICANT_PREPARATION
    return ReadinessLevel.NOT_READY


class CategoryScore(BaseModel):
    key: str
    label: str
    weight: int
    score: float | None = None  # 0-100; None when the interview did not cover it
    questions: int = 0

    @property
    def assessed(self) -> bool:
        return self.score is not None


class ArgumentSummary(BaseModel):
    category: str
    question_id: str
    summary: str
    score: int


class ReadinessResult(BaseModel):
    overall_score: int | None = None
    level: ReadinessLevel | None = None
    category_scores: list[CategoryScore] = Field(default_factory=list)
    coverage_pct: int = 0
    questions_answered: int = 0
    average_answer_score: float | None = None  # out of 20
    provisional: bool = True
    strongest_category: str | None = None
    weakest_category: str | None = None
    highest_risk_topic: str | None = None
    most_credible_argument: ArgumentSummary | None = None
    weakest_argument: ArgumentSummary | None = None

    def assessed(self) -> list[CategoryScore]:
        return [c for c in self.category_scores if c.assessed]


class PracticePriority(BaseModel):
    category_key: str
    category: str
    score: float | None = None
    weakness: str
    why_investors_care: str
    missing_evidence: str
    recommended_action: str
    practice_question: str


class AreaSummary(BaseModel):
    label: str
    score: float | None = None
    note: str = ""


class FinalAssessment(BaseModel):
    readiness: ReadinessResult
    investor_assessment: str
    strongest_areas: list[AreaSummary] = Field(default_factory=list)
    weakest_areas: list[AreaSummary] = Field(default_factory=list)
    investor_concerns: list[str] = Field(default_factory=list)
    questions_to_practice: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    practice_plan: list[PracticePriority] = Field(default_factory=list)
    narrative_source: Literal["llm", "heuristic"] = "heuristic"

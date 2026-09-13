"""Interview categories, questions, responses and sequential interview state."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.scoring import DimensionScores


class Category(str, Enum):
    MARKET = "market"
    PROBLEM = "problem"
    PRODUCT_SOLUTION = "product_solution"
    CUSTOMER = "customer"
    TRACTION = "traction"
    BUSINESS_MODEL = "business_model"
    UNIT_ECONOMICS = "unit_economics"
    GO_TO_MARKET = "go_to_market"
    COMPETITION = "competition"
    DEFENSIBILITY = "defensibility"
    TEAM = "team"
    FINANCIAL_PERFORMANCE = "financial_performance"
    FINANCIAL_PROJECTIONS = "financial_projections"
    RISKS = "risks"
    FUNDRAISING = "fundraising"
    VALUATION = "valuation"
    USE_OF_FUNDS = "use_of_funds"
    MILESTONES = "milestones"
    EXIT_POTENTIAL = "exit_potential"

    @property
    def label(self) -> str:
        return CATEGORY_LABELS[self]


CATEGORY_LABELS: dict[Category, str] = {
    Category.MARKET: "Market",
    Category.PROBLEM: "Problem",
    Category.PRODUCT_SOLUTION: "Product / Solution",
    Category.CUSTOMER: "Customer",
    Category.TRACTION: "Traction",
    Category.BUSINESS_MODEL: "Business Model",
    Category.UNIT_ECONOMICS: "Unit Economics",
    Category.GO_TO_MARKET: "Go-to-Market",
    Category.COMPETITION: "Competition",
    Category.DEFENSIBILITY: "Defensibility",
    Category.TEAM: "Team",
    Category.FINANCIAL_PERFORMANCE: "Financial Performance",
    Category.FINANCIAL_PROJECTIONS: "Financial Projections",
    Category.RISKS: "Risks",
    Category.FUNDRAISING: "Fundraising",
    Category.VALUATION: "Valuation",
    Category.USE_OF_FUNDS: "Use of Funds",
    Category.MILESTONES: "Milestones",
    Category.EXIT_POTENTIAL: "Exit Potential",
}

#: Natural position of a topic in a meeting: understanding first, evidence next, IC-level last.
CATEGORY_PHASE: dict[Category, int] = {
    Category.PROBLEM: 1, Category.CUSTOMER: 1, Category.PRODUCT_SOLUTION: 1, Category.MARKET: 1,
    Category.BUSINESS_MODEL: 1, Category.TEAM: 1,
    Category.TRACTION: 2, Category.UNIT_ECONOMICS: 2, Category.GO_TO_MARKET: 2, Category.COMPETITION: 2,
    Category.DEFENSIBILITY: 2, Category.FINANCIAL_PERFORMANCE: 2,
    Category.FINANCIAL_PROJECTIONS: 3, Category.RISKS: 3, Category.FUNDRAISING: 3, Category.VALUATION: 3,
    Category.USE_OF_FUNDS: 3, Category.MILESTONES: 3, Category.EXIT_POTENTIAL: 3,
}


class Difficulty(IntEnum):
    UNDERSTANDING = 1
    EVIDENCE = 2
    INVESTMENT_COMMITTEE = 3

    @property
    def label(self) -> str:
        return DIFFICULTY_LABELS[self]


DIFFICULTY_LABELS: dict[Difficulty, str] = {
    Difficulty.UNDERSTANDING: "Level 1 · Understanding",
    Difficulty.EVIDENCE: "Level 2 · Evidence",
    Difficulty.INVESTMENT_COMMITTEE: "Level 3 · Investment Committee",
}


def _now() -> datetime:
    return datetime.now(UTC)


class Question(BaseModel):
    question_id: str
    category: Category
    text: str
    difficulty: Difficulty
    is_follow_up: bool = False
    parent_question_id: str | None = None
    issue_ids: list[str] = Field(default_factory=list)
    intent: str = ""
    source: Literal["llm", "template"] = "template"
    asked_at: datetime = Field(default_factory=_now)


class ResponseRecord(BaseModel):
    question_id: str
    category: Category
    question: str
    difficulty: Difficulty
    is_follow_up: bool = False
    founder_answer: str
    scores: DimensionScores
    investor_assessment: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommended_answer_structure: str = ""
    model_answer: str = ""
    follow_up_required: bool = False
    follow_up_reason: str = ""
    founder_claims: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    placeholders_inserted: int = 0
    scoring_source: Literal["llm", "heuristic"] = "heuristic"
    answered_at: datetime = Field(default_factory=_now)

    def to_record(self) -> dict[str, Any]:
        """The session data-model representation of this response."""
        return {
            "question_id": self.question_id,
            "category": self.category.value,
            "question": self.question,
            "difficulty": int(self.difficulty),
            "is_follow_up": self.is_follow_up,
            "founder_answer": self.founder_answer,
            "scores": self.scores.model_dump(),
            "investor_assessment": self.investor_assessment,
            "strengths": list(self.strengths),
            "weaknesses": list(self.weaknesses),
            "recommended_answer_structure": self.recommended_answer_structure,
            "model_answer": self.model_answer,
            "follow_up_required": self.follow_up_required,
            "founder_claims": list(self.founder_claims),
            "scoring_source": self.scoring_source,
        }


class TopicPlan(BaseModel):
    category: Category
    priority: float
    issue_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class InterviewState(BaseModel):
    plan: list[TopicPlan] = Field(default_factory=list)
    asked: list[Question] = Field(default_factory=list)
    responses: list[ResponseRecord] = Field(default_factory=list)
    current: Question | None = None
    followups_by_category: dict[str, int] = Field(default_factory=dict)
    covered_categories: list[Category] = Field(default_factory=list)
    resolved_issue_ids: list[str] = Field(default_factory=list)
    max_questions: int = 12
    completed: bool = False
    ended_early: bool = False

    @property
    def answered(self) -> int:
        return len(self.responses)

    @property
    def progress_pct(self) -> int:
        return min(100, round(100 * self.answered / max(1, self.max_questions)))

    def response_for(self, question_id: str) -> ResponseRecord | None:
        return next((r for r in self.responses if r.question_id == question_id), None)

    def question(self, question_id: str) -> Question | None:
        return next((q for q in self.asked if q.question_id == question_id), None)

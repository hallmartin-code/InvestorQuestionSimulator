"""Answer scoring.

The model proposes four 0-5 dimension scores against a fixed rubric, together with its
coaching (one call). Python clamps the scores and computes the total and percentage. When the
model is unavailable, a deterministic heuristic scorer produces the four dimensions instead.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.analysis.evidence_mapper import category_digest
from app.integrations.llm_client import StructuredLLM
from app.models.company import CompanyProfile
from app.models.interview import InterviewState, Question
from app.models.scoring import MAX_DIMENSION, MAX_RESPONSE_SCORE, DimensionScores
from app.utils.text import content_tokens, first_sentence, truncate

MAX_ANSWER_CHARS = 6000

RUBRIC = """Scoring rubric (integers 0-5, scored as a demanding investor would):
CLARITY - 5: direct answer in the first sentence, easy to follow. 3: answers, but meanders or buries the point. 1: evasive or confusing. 0: no answer.
EVIDENCE - 5: specific metrics, customer proof, contracts, research or financial data. 3: some figures or examples, but thin or unsourced. 1: assertion only. 0: none.
CREDIBILITY - 5: believable assumptions, consistent with the deck and earlier answers. 3: plausible but optimistic or partly unsupported. 1: contradicts the deck or earlier answers, or is implausible. 0: not credible.
COMPLETENESS - 5: every part of the question addressed. 3: the main part addressed, secondary parts missed. 1: most of the question ignored. 0: nothing relevant."""


class EvaluationOutput(BaseModel):
    clarity: int = Field(description="0-5")
    evidence: int = Field(description="0-5")
    credibility: int = Field(description="0-5")
    completeness: int = Field(description="0-5")
    investor_assessment: str = Field(description="Two or three sentences: how a demanding investor reads this answer.")
    what_worked: list[str] = Field(description="Specific strong elements of the answer; may be empty.")
    needs_improvement: list[str] = Field(description="Missing evidence, vagueness, unsupported assumptions or poor framing.")
    better_answer_structure: str = Field(description="How to structure a stronger answer, as short ordered steps.")
    model_answer: str = Field(description="An improved example answer in the founder's voice; placeholders for unavailable figures.")
    founder_claims: list[str] = Field(description="New factual claims the founder made in this answer.")
    contradictions: list[str] = Field(description="Conflicts between this answer and the deck, profile or earlier answers.")
    follow_up_required: bool
    follow_up_reason: str = Field(description="Why a follow-up is or is not needed, in one sentence.")
    follow_up_question: str = Field(description="The single most useful follow-up question on this topic.")


EVALUATION_INSTRUCTIONS = """Task: evaluate the founder's answer to your interview question and coach them.

Question ({category}, {difficulty}):
{question}

What the question was testing: {intent}

Company profile entries for this topic:
{digest}

Earlier in this interview:
{transcript}

<founder_answer>
{answer}
</founder_answer>

{rubric}

Coaching rules:
- The investor assessment explains how a demanding investor would read this specific answer.
- Figures the founder gives in the answer are founder claims. Note any contradiction with the deck, the profile or earlier answers.
- The model answer improves communication without manufacturing evidence: use only facts from the deck, the profile and the founder's answers, and put bracketed placeholders such as [ARR], [customer count], [gross margin], [market source] or [conversion rate] wherever a figure is not available. Never make up numbers, customers, partners or sources.
- Set follow_up_required to true when the answer leaves an important issue unresolved: missing evidence, a contradiction, evasion or an unrealistic assumption. The follow-up question must probe that issue and must not repeat a question already asked:
{asked}"""


def clamp_dimension(value: object) -> int:
    try:
        number = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_DIMENSION, number))


def scores_from_values(clarity: object, evidence: object, credibility: object, completeness: object) -> DimensionScores:
    return DimensionScores(clarity=clamp_dimension(clarity), evidence=clamp_dimension(evidence),
                           credibility=clamp_dimension(credibility), completeness=clamp_dimension(completeness))


def response_score(scores: DimensionScores) -> int:
    return scores.clarity + scores.evidence + scores.credibility + scores.completeness


def response_percentage(scores: DimensionScores) -> float:
    return round(response_score(scores) / MAX_RESPONSE_SCORE * 100, 1)


# --- Heuristic scorer --------------------------------------------------------------------

_HEDGES = re.compile(
    r"(?i)\b(i think|i guess|maybe|probably|hopefully|not sure|kind of|sort of|we believe|roughly|"
    r"i don'?t know|no idea|hard to say|we don'?t (?:really )?(?:track|know|calculate|measure|have))\b"
)
_NON_ANSWER = re.compile(r"(?i)^\s*(?:i don'?t know|no idea|not sure|n/?a|pass|skip|no comment|tbd)[.!]?\s*$")
_EVIDENCE_WORDS = re.compile(
    r"(?i)\b(customers?|contracts?|signed|pilots?|revenue|arr|mrr|retention|churn|margins?|cac|ltv|payback|"
    r"source|data|study|survey|lois?|paid|paying|conversion|cohorts?|benchmark|report|interviews?|renewals?)\b"
)
_ABSOLUTES = re.compile(r"(?i)\b(no competitors?|no competition|guaranteed|can'?t fail|everyone needs|zero risk)\b")
_FIGURE = re.compile(r"[$€£]?\d[\d,.]*%?")
_QUESTION_PARTS = re.compile(r"(?i),\s*(?:and\s+)?(?:how|what|why|who|when|which|where)\b|\band (?:how|what|why|who|when|which)\b")


def heuristic_scores(question: str, answer: str) -> DimensionScores:
    """Deterministic proxy scores: directness, figures and evidence words, hedging, topic coverage."""
    text = (answer or "").strip()
    words = len(text.split())
    if words == 0:
        return DimensionScores(clarity=0, evidence=0, credibility=0, completeness=0)
    if words < 4 or _NON_ANSWER.match(text):
        return DimensionScores(clarity=1, evidence=0, credibility=1, completeness=0)

    hedges = len(_HEDGES.findall(text))
    figures = len(_FIGURE.findall(text))
    evidence_hits = len({m.lower().rstrip("s") for m in _EVIDENCE_WORDS.findall(text)})
    lead_words = len(first_sentence(text, 1000).split())

    clarity = 2 + (1 if 20 <= words <= 250 else 0) + (1 if lead_words <= 30 else 0) + (1 if hedges == 0 else 0)
    clarity -= 1 if words > 400 else 0
    evidence = min(3, figures) + min(2, evidence_hits // 2)
    credibility = 3 + (1 if figures >= 2 and evidence_hits >= 2 else 0) - min(2, hedges)
    credibility -= 2 if _ABSOLUTES.search(text) else 0

    question_terms = content_tokens(question)
    coverage = len(question_terms & content_tokens(text)) / len(question_terms) if question_terms else 0.5
    completeness = round(5 * min(1.0, coverage * 1.6))
    parts = max(1, question.count("?")) + len(_QUESTION_PARTS.findall(question))
    if words < 25:
        completeness = min(completeness, 2)
    if parts >= 2 and words < 60:
        completeness = min(completeness, 3)

    return scores_from_values(clarity, evidence, credibility, completeness)


# --- Model evaluation ----------------------------------------------------------------------

def transcript_digest(state: InterviewState, *, limit: int = 6) -> str:
    if not state.responses:
        return "- This is the first question."
    lines: list[str] = []
    for index, response in enumerate(state.responses[-limit:], start=max(1, len(state.responses) - limit + 1)):
        lines.append(f"Q{index} [{response.category.label}, level {int(response.difficulty)}]: {response.question}")
        lines.append(f"A{index} (scored {response.scores.total}/20): {truncate(response.founder_answer, 350)}")
    return "\n".join(lines)


def evaluate_answer(*, llm: StructuredLLM, effort: str | None, context: str, profile: CompanyProfile,
                    question: Question, answer: str, state: InterviewState) -> tuple[EvaluationOutput | None, str | None]:
    asked = "\n".join(f"- {q.text}" for q in state.asked) or "- none"
    instructions = EVALUATION_INSTRUCTIONS.format(
        category=question.category.label, difficulty=question.difficulty.label, question=question.text,
        intent=question.intent or "command of this topic", digest=category_digest(profile, question.category),
        transcript=transcript_digest(state), answer=truncate(answer, MAX_ANSWER_CHARS), rubric=RUBRIC, asked=asked,
    )
    return llm.structured(label="evaluate_answer", output_model=EvaluationOutput, instructions=instructions,
                          context=context, effort=effort)

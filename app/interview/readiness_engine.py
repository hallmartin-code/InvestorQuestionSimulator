"""Readiness scoring, practice plan and the final assessment.

Arithmetic is deterministic:

* Category score = mean response percentage of the answers on that category's topics.
* Overall score = weighted mean over the categories the interview covered, using the
  published weights (which sum to 100). Uncovered categories are excluded and reported as
  "not assessed"; coverage (the share of weight assessed) is reported alongside, and a
  score with coverage below 60% or fewer than five answers is marked provisional.

The model may rewrite the narrative (assessment, concerns, next steps, practice-plan text)
for priorities Python has already chosen; figures in its text are guarded against the source.
"""

from __future__ import annotations

from statistics import mean

from pydantic import BaseModel, Field

from app.analysis.evidence_mapper import CATEGORY_FIELDS
from app.integrations.llm_client import StructuredLLM
from app.interview.coaching_engine import AllowedFigures, guard_figures
from app.models.analysis import GapAnalysis, IssueTag
from app.models.company import FIELD_SPECS, CompanyProfile
from app.models.interview import CATEGORY_LABELS, Category, ResponseRecord
from app.models.scoring import (
    READINESS_BY_KEY,
    READINESS_CATEGORIES,
    AreaSummary,
    ArgumentSummary,
    CategoryScore,
    FinalAssessment,
    PracticePriority,
    ReadinessResult,
    classify_readiness,
    readiness_category_for,
)
from app.utils.text import first_sentence, truncate

PROVISIONAL_COVERAGE = 60
PROVISIONAL_ANSWERS = 5
PRIORITY_THRESHOLD = 80
TAG_RISK = {IssueTag.RED_FLAG: 12, IssueTag.INCONSISTENCY: 8, IssueTag.EVIDENCE_GAP: 5, IssueTag.QUESTION: 2,
            IssueTag.STRENGTH: 0}

PRACTICE_LIBRARY: dict[str, dict[str, str]] = {
    "market_understanding": {
        "why": "Investors need to believe the market can support a venture-scale outcome and that the founder sized it rigorously.",
        "action": "Rebuild the market size bottom-up (target customers × annual price), cite a source for each input, and reconcile it with the top-down figure.",
        "question": "Walk me through how you calculated your market size and what portion is realistically reachable in five years.",
    },
    "problem_customer": {
        "why": "Without a sharply defined, costly problem and a clear buyer, investors cannot judge demand or sales efficiency.",
        "action": "Quantify the problem's cost for one named customer type and prepare two or three customer examples with specifics.",
        "question": "Who exactly buys, what does the problem cost them today, and how do you know?",
    },
    "product_differentiation": {
        "why": "Investors fund advantages that are hard to copy; a product described only by features reads as replicable.",
        "action": "Document the measurable customer outcome and the one capability that would take a competitor longest to replicate.",
        "question": "What measurable outcome does your product deliver, and what stops a well-funded competitor from copying it?",
    },
    "traction": {
        "why": "Traction is the strongest evidence of demand; vague or unverified traction lowers conviction more than almost anything else.",
        "action": "Prepare a one-page metrics sheet: customers, revenue or ARR by month, growth over a stated period, retention and pipeline.",
        "question": "Break down your traction: how many paying customers, what revenue, and how has it trended over twelve months?",
    },
    "business_model_economics": {
        "why": "Without acquisition economics and margins, investors cannot tell whether growth is capital-efficient.",
        "action": "Calculate CAC as total sales and marketing spend divided by new customers in the same period, then LTV from margin and churn, and payback in months.",
        "question": "What does it cost you to acquire one customer, and how many months does it take to recover that cost?",
    },
    "go_to_market": {
        "why": "Investors need to see a repeatable way to acquire customers that can scale with the capital raised.",
        "action": "Map your funnel with real conversion rates and sales-cycle length, and identify the channel that will scale.",
        "question": "Walk me through your sales process and the conversion rate at each stage.",
    },
    "competitive_position": {
        "why": "Claims of no competition or thin differentiation signal a misread market.",
        "action": "Build an honest competitive map including the status quo, with when you win, when you lose, and why.",
        "question": "Who do customers choose instead of you, and when do you lose?",
    },
    "team": {
        "why": "Early-stage investors back teams; they need to see founder-market fit and a plan for key gaps.",
        "action": "Prepare a concise founder-market-fit story and a hiring plan for the most critical missing role.",
        "question": "Why is this team uniquely positioned to win, and what critical role is missing?",
    },
    "financial_command": {
        "why": "Founders who cannot state burn, margin and the drivers of their forecast lose credibility quickly.",
        "action": "Memorize trailing-twelve-month revenue, burn and margin, and the three assumptions that drive the forecast.",
        "question": "What are the three assumptions that drive most of your projected growth?",
    },
    "fundraising_strategy": {
        "why": "Investors test whether the round size, valuation and use of funds buy a milestone that makes the next round financeable.",
        "action": "Tie each use-of-funds line to a milestone, show months of runway, and prepare the rationale for your valuation.",
        "question": "What milestone does this financing purchase, and why is the valuation justified today?",
    },
    "risk_awareness": {
        "why": "Founders who name their risks and mitigations earn trust; those who deny risk raise concern.",
        "action": "List the top three risks with a mitigation and a measurable leading indicator for each.",
        "question": "What is the biggest risk to this business, and how will you know your mitigation is working?",
    },
}


# --- Scores ------------------------------------------------------------------------------

def category_scores(responses: list[ResponseRecord]) -> list[CategoryScore]:
    scores: list[CategoryScore] = []
    for rc in READINESS_CATEGORIES:
        relevant = [r for r in responses if r.category.value in rc.categories]
        score = round(mean(r.scores.percentage for r in relevant), 1) if relevant else None
        scores.append(CategoryScore(key=rc.key, label=rc.label, weight=rc.weight, score=score, questions=len(relevant)))
    return scores


def _argument(response: ResponseRecord) -> ArgumentSummary:
    return ArgumentSummary(category=response.category.label, question_id=response.question_id,
                           summary=first_sentence(response.founder_answer, 200), score=response.scores.total)


def highest_risk_topic(responses: list[ResponseRecord], gaps: GapAnalysis | None, resolved: list[str]) -> str | None:
    risk: dict[Category, float] = {}
    by_category: dict[Category, list[ResponseRecord]] = {}
    for response in responses:
        by_category.setdefault(response.category, []).append(response)
    open_issues = [i for i in (gaps.issues if gaps else []) if i.is_concern and i.issue_id not in resolved]
    for category in set(by_category) | {i.category for i in open_issues}:
        answered = by_category.get(category)
        base = 100 - mean(r.scores.percentage for r in answered) if answered else 50
        risk[category] = base + sum(TAG_RISK[i.tag] * i.severity for i in open_issues if i.category is category)
    if not risk:
        return None
    worst = max(sorted(risk, key=lambda c: c.value), key=lambda c: risk[c])
    return CATEGORY_LABELS[worst]


def calculate_readiness(responses: list[ResponseRecord], gaps: GapAnalysis | None = None,
                        resolved_issue_ids: list[str] | None = None) -> ReadinessResult:
    scores = category_scores(responses)
    assessed = [c for c in scores if c.assessed]
    result = ReadinessResult(category_scores=scores, questions_answered=len(responses))
    if not assessed:
        return result
    weight = sum(c.weight for c in assessed)
    overall = round(sum(c.weight * (c.score or 0.0) for c in assessed) / weight)
    result.overall_score = overall
    result.level = classify_readiness(overall)
    result.coverage_pct = weight
    result.average_answer_score = round(mean(r.scores.total for r in responses), 1)
    result.provisional = weight < PROVISIONAL_COVERAGE or len(responses) < PROVISIONAL_ANSWERS
    result.strongest_category = max(assessed, key=lambda c: (c.score, c.weight)).label
    result.weakest_category = min(assessed, key=lambda c: (c.score, -c.weight)).label
    result.highest_risk_topic = highest_risk_topic(responses, gaps, resolved_issue_ids or [])
    result.most_credible_argument = _argument(
        max(responses, key=lambda r: (r.scores.credibility + r.scores.evidence, r.scores.total)))
    result.weakest_argument = _argument(
        min(responses, key=lambda r: (r.scores.credibility + r.scores.evidence, r.scores.total)))
    return result


# --- Practice plan -----------------------------------------------------------------------------

def _missing_evidence(key: str, profile: CompanyProfile, responses: list[ResponseRecord]) -> str:
    rc = READINESS_BY_KEY[key]
    missing: list[str] = []
    for value in rc.categories:
        for name in CATEGORY_FIELDS[Category(value)]:
            spec = FIELD_SPECS[name]
            if not spec.calculated and profile.get(name) is None and spec.label not in missing:
                missing.append(spec.label)
    weak_evidence = any(r.scores.evidence <= 2 for r in responses if r.category.value in rc.categories)
    parts = []
    if missing:
        parts.append("Not in the deck: " + ", ".join(missing[:5]))
    if weak_evidence:
        parts.append("answers lacked supporting figures or proof")
    return "; ".join(parts) or "Supporting detail was thin relative to what investors expect."


def build_practice_plan(readiness: ReadinessResult, responses: list[ResponseRecord], gaps: GapAnalysis | None,
                        profile: CompanyProfile, *, limit: int = 5) -> list[PracticePriority]:
    assessed = sorted(readiness.assessed(), key=lambda c: (c.score, -c.weight))
    chosen = [c for c in assessed if (c.score or 0) < PRIORITY_THRESHOLD][:limit]
    for c in assessed:
        if len(chosen) >= 3:
            break
        if c not in chosen:
            chosen.append(c)
    if len(chosen) < 3 and gaps is not None:
        concern_keys = []
        for issue in gaps.concerns():
            key = readiness_category_for(issue.category).key
            if key not in concern_keys:
                concern_keys.append(key)
        for key in concern_keys:
            if len(chosen) >= 3:
                break
            if all(c.key != key for c in chosen):
                chosen.append(next(c for c in readiness.category_scores if c.key == key))

    plan: list[PracticePriority] = []
    for category_score in chosen[:limit]:
        rc = READINESS_BY_KEY[category_score.key]
        library = PRACTICE_LIBRARY[rc.key]
        relevant = [r for r in responses if r.category.value in rc.categories]
        weakest = min(relevant, key=lambda r: r.scores.total, default=None)
        if weakest is not None and weakest.weaknesses:
            weakness = truncate(weakest.weaknesses[0], 220)
        elif category_score.score is not None:
            weakness = f"Answers on {rc.label.lower()} averaged {category_score.score:.0f}/100."
        else:
            weakness = f"{rc.label} was not covered in the interview, and the deck analysis flagged concerns."
        practice_question = weakest.question if weakest is not None and weakest.scores.percentage < 60 else library["question"]
        plan.append(PracticePriority(
            category_key=rc.key, category=rc.label, score=category_score.score, weakness=weakness,
            why_investors_care=library["why"], missing_evidence=_missing_evidence(rc.key, profile, relevant),
            recommended_action=library["action"], practice_question=truncate(practice_question, 400),
        ))
    return plan


# --- Final assessment ----------------------------------------------------------------------------

class PracticeItemOutput(BaseModel):
    category_key: str
    weakness: str
    why_investors_care: str
    missing_evidence: str
    recommended_action: str
    practice_question: str


class SummaryOutput(BaseModel):
    investor_assessment: str = Field(description="Two to four sentences for the report's Investor Assessment.")
    investor_concerns: list[str] = Field(description="Three to five specific investor concerns, most serious first.")
    next_steps: list[str] = Field(description="Exactly three concrete, specific next steps.")
    practice_plan: list[PracticeItemOutput]


SUMMARY_INSTRUCTIONS = """Task: write the narrative for this founder's Fundraising Interview Readiness Report. The scores below were calculated by the application; do not change or recompute them.

Company: {company}
Overall readiness: {overall}/100 — {level}{provisional}
Coverage: {coverage}% of readiness weight assessed; {answered} answers; average answer score {average}/20.
Category scores:
{categories}

Answers (question, score, assessor notes):
{answers}

Open findings from the deck analysis:
{issues}

Practice priorities selected by the application (keep these category_key values, in this order):
{priorities}

Rules:
- The investor assessment is 2-4 sentences, specific to this company and interview, written for an investor audience.
- Concerns and next steps must come from the answers and findings above. Next steps are concrete actions the founder can take before investor meetings.
- For each practice priority, tailor weakness, why investors care, missing evidence, recommended action and a practice question to this company.
- Use only figures that appear in the material or above. Do not invent metrics, customers, investors or sources."""


def _deterministic_concerns(readiness: ReadinessResult, responses: list[ResponseRecord], gaps: GapAnalysis | None,
                            resolved: list[str]) -> list[str]:
    concerns: list[str] = []
    if gaps is not None:
        for issue in gaps.concerns():
            if issue.issue_id in resolved or (issue.tag is IssueTag.QUESTION and len(concerns) >= 2):
                continue
            concerns.append(truncate(f"{issue.title}: {issue.detail}", 200))
            if len(concerns) >= 3:
                break
    seen_weaknesses: set[str] = set()
    for response in sorted(responses, key=lambda r: r.scores.total):
        if len(concerns) >= 5:
            break
        if response.scores.percentage < 60 and response.weaknesses:
            weakness = response.weaknesses[0]
            if weakness in seen_weaknesses:
                continue  # one concern per distinct weakness, not one per topic
            seen_weaknesses.add(weakness)
            concerns.append(truncate(f"{response.category.label}: {weakness}", 200))
    return concerns[:5]


def _areas(readiness: ReadinessResult, responses: list[ResponseRecord]) -> tuple[list[AreaSummary], list[AreaSummary]]:
    assessed = sorted(readiness.assessed(), key=lambda c: (-(c.score or 0), -c.weight))
    strong_count = min(3, (len(assessed) + 1) // 2)
    strong, weak_pool = assessed[:strong_count], assessed[strong_count:]
    weak = sorted(weak_pool, key=lambda c: ((c.score or 0), -c.weight))[:3]

    def note(key: str, best: bool) -> str:
        rc = READINESS_BY_KEY[key]
        relevant = [r for r in responses if r.category.value in rc.categories]
        if not relevant:
            return ""
        pick = max(relevant, key=lambda r: r.scores.total) if best else min(relevant, key=lambda r: r.scores.total)
        items = pick.strengths if best else pick.weaknesses
        return truncate(items[0], 140) if items else ""

    return ([AreaSummary(label=c.label, score=c.score, note=note(c.key, True)) for c in strong],
            [AreaSummary(label=c.label, score=c.score, note=note(c.key, False)) for c in weak])


def deterministic_assessment(company: str, readiness: ReadinessResult, lead_finding: str | None) -> str:
    if readiness.overall_score is None or readiness.level is None:
        return (f"{company} has not answered enough interview questions for a readiness assessment. "
                "Complete at least a few questions to receive scores and coaching.")
    text = (f"{company} scored {readiness.overall_score}/100 ({readiness.level.value.lower()}) across "
            f"{readiness.questions_answered} answers covering {readiness.coverage_pct}% of the readiness weighting.")
    if readiness.strongest_category and readiness.weakest_category and \
            readiness.strongest_category != readiness.weakest_category:
        text += (f" The strongest area was {readiness.strongest_category} and the weakest was "
                 f"{readiness.weakest_category}.")
    if lead_finding:
        text += f" The most significant open finding from the deck review: {truncate(lead_finding, 90)}."
    if readiness.provisional:
        text += " Treat the score as provisional: the interview did not cover enough topics."
    return text


def build_final_assessment(*, company: str, readiness: ReadinessResult, responses: list[ResponseRecord],
                           gaps: GapAnalysis | None, profile: CompanyProfile, resolved_issue_ids: list[str],
                           llm: StructuredLLM | None, context: str, effort: str | None,
                           allowed_text: str) -> tuple[FinalAssessment, str | None]:
    plan = build_practice_plan(readiness, responses, gaps, profile)
    concerns = _deterministic_concerns(readiness, responses, gaps, resolved_issue_ids)
    strongest, weakest = _areas(readiness, responses)
    lead_finding = next((i.title for i in (gaps.concerns() if gaps else []) if i.issue_id not in resolved_issue_ids),
                        None)
    assessment = FinalAssessment(
        readiness=readiness,
        investor_assessment=deterministic_assessment(company, readiness, lead_finding),
        strongest_areas=strongest, weakest_areas=weakest, investor_concerns=concerns,
        questions_to_practice=[p.practice_question for p in plan[:3]],
        next_steps=[p.recommended_action for p in plan[:3]],
        practice_plan=plan,
    )
    if llm is None or not responses or readiness.overall_score is None:
        return assessment, None

    allowed = AllowedFigures.from_text(allowed_text + "\n" + _scores_text(readiness))
    instructions = SUMMARY_INSTRUCTIONS.format(
        company=company, overall=readiness.overall_score, level=readiness.level.value if readiness.level else "",
        provisional=" (provisional)" if readiness.provisional else "", coverage=readiness.coverage_pct,
        answered=readiness.questions_answered, average=readiness.average_answer_score,
        categories=_scores_text(readiness),
        answers="\n".join(f"- [{r.category.label}] {r.question} — {r.scores.total}/20. {truncate(r.investor_assessment, 240)}"
                          for r in responses),
        issues="\n".join(f"- [{i.tag.value}] {i.title}: {i.detail}" for i in (gaps.concerns() if gaps else [])
                         if i.issue_id not in resolved_issue_ids)[:4000] or "- none",
        priorities="\n".join(f"- {p.category_key} ({p.category}, score {p.score if p.score is not None else 'not assessed'})"
                             for p in plan),
    )
    output, error = llm.structured(label="final_summary", output_model=SummaryOutput, instructions=instructions,
                                   context=context, effort=effort)
    if output is None:
        return assessment, f"AI report narrative was unavailable ({error}); a standard narrative was used."

    def g(text: str, limit: int) -> str:
        return guard_figures(truncate(text, limit), allowed)[0]

    assessment.investor_assessment = g(output.investor_assessment, 900) or assessment.investor_assessment
    llm_concerns = [g(c, 220) for c in output.investor_concerns if c.strip()][:5]
    if len(llm_concerns) >= 3:
        assessment.investor_concerns = llm_concerns
    steps = [g(s, 220) for s in output.next_steps if s.strip()][:3]
    if len(steps) == 3:
        assessment.next_steps = steps
    by_key = {item.category_key: item for item in output.practice_plan}
    for priority in assessment.practice_plan:
        item = by_key.get(priority.category_key)
        if item is None:
            continue
        priority.weakness = g(item.weakness, 260) or priority.weakness
        priority.why_investors_care = g(item.why_investors_care, 260) or priority.why_investors_care
        priority.missing_evidence = g(item.missing_evidence, 260) or priority.missing_evidence
        priority.recommended_action = g(item.recommended_action, 300) or priority.recommended_action
        priority.practice_question = g(item.practice_question, 400) or priority.practice_question
    assessment.questions_to_practice = [p.practice_question for p in assessment.practice_plan[:3]]
    if len(steps) != 3:
        assessment.next_steps = [p.recommended_action for p in assessment.practice_plan[:3]]
    assessment.narrative_source = "llm"
    return assessment, None


def _scores_text(readiness: ReadinessResult) -> str:
    return "\n".join(f"- {c.label} (weight {c.weight}): " + (f"{c.score:g}/100" if c.score is not None else "not assessed")
                     for c in readiness.category_scores)

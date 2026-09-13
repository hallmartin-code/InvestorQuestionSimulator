"""Coaching: investor assessment, strengths, improvements, answer structure and a model answer.

The model's coaching is passed through a figure guard: any number in the coaching text that
does not appear in the deck, the founder's inputs or the founder's answers is replaced with a
bracketed placeholder, so a model answer can improve communication but never manufacture
evidence. Without the model, category templates produce the same sections, filled only with
non-inferred profile values and placeholders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.interview.scoring_engine import EvaluationOutput
from app.models.company import CompanyProfile, DataStatus
from app.models.interview import Category
from app.models.scoring import DimensionScores
from app.utils.money import money_mentions, parse_money
from app.utils.text import numbers_in, truncate

FIGURE_RE = re.compile(
    r"(?<![A-Za-z0-9.\[\-])"
    r"(?P<cur>[$€£]\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:(?P<suffix>\s?(?:%|x|thousand|million|billion|mm|mn|bn|[kmb]))(?![A-Za-z0-9])|(?![A-Za-z0-9%]|[.,]\d))",
    re.IGNORECASE,
)
_MONEY_SUFFIX = re.compile(r"(?i)thousand|million|billion|mm|mn|bn|^[kmb]$")


@dataclass
class AllowedFigures:
    numbers: set[str]
    amounts: list[float]

    @classmethod
    def from_text(cls, text: str) -> AllowedFigures:
        amounts = [value for _t, value, _o in money_mentions(text)]
        amounts += [float(n) for n in numbers_in(text) if float(n) >= 1000]
        return cls(numbers=numbers_in(text), amounts=amounts)


def _placeholder(cur: str | None, suffix: str) -> str:
    suffix = suffix.strip().lower()
    if suffix == "%":
        return "[percentage]"
    if suffix == "x":
        return "[multiple]"
    if cur or _MONEY_SUFFIX.search(suffix or "-"):
        return "[amount]"
    return "[figure]"


def _figure_allowed(match: re.Match[str], allowed: AllowedFigures) -> bool:
    num = match.group("num").rstrip(",")
    cur, suffix = match.group("cur"), (match.group("suffix") or "").strip()
    canonical = numbers_in(num)
    if not cur and not suffix:
        value = float(num.replace(",", ""))
        if value <= 10 and "." not in num:
            return True  # small counts such as "three assumptions" or list numbering
    if canonical and canonical <= allowed.numbers:
        return True
    if cur or (suffix and _MONEY_SUFFIX.search(suffix)):
        amount = parse_money(match.group(0))
        if amount is not None and any(abs(amount - a) <= 0.01 * max(a, 1.0) for a in allowed.amounts):
            return True
    return False


def guard_figures(text: str, allowed: AllowedFigures) -> tuple[str, int]:
    """Replace figures not traceable to the allowed material with placeholders."""
    if not text:
        return "", 0
    replaced = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal replaced
        if _figure_allowed(match, allowed):
            return match.group(0)
        num = match.group("num")
        if not match.group("cur") and not match.group("suffix") and re.fullmatch(r"(19|20)\d{2}", num):
            replaced += 1
            return "[year]"
        replaced += 1
        return _placeholder(match.group("cur"), match.group("suffix") or "")

    return FIGURE_RE.sub(substitute, text), replaced


@dataclass
class Coaching:
    investor_assessment: str
    strengths: list[str]
    weaknesses: list[str]
    answer_structure: str
    model_answer: str
    follow_up_required: bool = False
    follow_up_reason: str = ""
    follow_up_question: str | None = None
    founder_claims: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    placeholders_inserted: int = 0


def _clean_list(items: list[str], limit: int = 5, chars: int = 300) -> list[str]:
    return [truncate(i, chars) for i in items if i and i.strip()][:limit]


def coaching_from_model(evaluation: EvaluationOutput, allowed: AllowedFigures) -> Coaching:
    total = 0

    def g(text: str) -> str:
        nonlocal total
        guarded, count = guard_figures(text, allowed)
        total += count
        return guarded

    coaching = Coaching(
        investor_assessment=g(truncate(evaluation.investor_assessment, 900)),
        strengths=[g(s) for s in _clean_list(evaluation.what_worked)],
        weaknesses=[g(s) for s in _clean_list(evaluation.needs_improvement + evaluation.contradictions, limit=6)],
        answer_structure=g(truncate(evaluation.better_answer_structure, 900)),
        model_answer=g(truncate(evaluation.model_answer, 2000)),
        follow_up_required=bool(evaluation.follow_up_required),
        follow_up_reason=truncate(evaluation.follow_up_reason, 300),
        follow_up_question=truncate(evaluation.follow_up_question, 600) or None,
        founder_claims=_clean_list(evaluation.founder_claims, limit=8, chars=240),
        contradictions=_clean_list(evaluation.contradictions, limit=4),
    )
    coaching.placeholders_inserted = total
    return coaching


# --- Heuristic coaching --------------------------------------------------------------------

MODEL_ANSWER_TEMPLATES: dict[Category, str] = {
    Category.MARKET: "We size the market bottom-up. There are [number of target customers] in our initial segment, "
                     "each paying about {pricing|[annual contract value]}, which gives a serviceable market of "
                     "{sam|[SAM]}. Our top-down figure of {tam|[TAM]} comes from {market_source|[market source]}. "
                     "Over the next five years we can realistically reach [share of segment], because [reason tied "
                     "to go-to-market capacity].",
    Category.PROBLEM: "{target_customer|[Target customers]} face [specific problem], which costs them [quantified cost] "
                      "a year. We validated this through [number of customer interviews or data source], and "
                      "[example customer] told us [specific evidence]. Existing approaches fail because [reason].",
    Category.PRODUCT_SOLUTION: "Today the product [does what, for whom]. A typical customer uses it to [workflow], and "
                               "the measurable result is [outcome metric] against [baseline]. The hard part to "
                               "replicate is [technical or data advantage].",
    Category.CUSTOMER: "The buyer is [title] at {target_customer|[customer type]}, the user is [title], and [title] "
                       "signs out of the [budget line] budget. Our recent wins took [sales cycle] on average and came "
                       "through [channel].",
    Category.TRACTION: "We have {customer_count|[customer count]} paying customers and {arr|[ARR]} in ARR, growing "
                       "{revenue_growth|[growth rate]} over [period]. [Share of revenue] is recurring, net revenue "
                       "retention is {retention|[NRR]}, and our largest customer is [share] of revenue. Our strongest "
                       "leading indicator is [pipeline or usage metric].",
    Category.BUSINESS_MODEL: "We charge {pricing|[price and pricing unit]}, paid by [buyer] on [contract terms]. Gross "
                             "margin is {gross_margin|[gross margin]} today and should reach [target margin] at scale "
                             "because [driver]. The assumption we are testing hardest is [assumption], through [test].",
    Category.UNIT_ECONOMICS: "Our blended CAC is {cac|[CAC]}: [sales and marketing spend] over [period] divided by "
                             "[new customers] acquired in that period. LTV is {ltv|[LTV]}, based on [gross margin] and "
                             "[annual churn], so LTV:CAC is {ltv_cac_ratio|[ratio]} and payback is [months].",
    Category.GO_TO_MARKET: "We sell through [channel] to [buyer]. A typical deal moves from first meeting to signature "
                           "in {sales_cycle|[sales cycle]}, with [conversion rate] from qualified opportunity to close. "
                           "[Team size] people run the process today, and the next customers will come from [channel] "
                           "because [evidence].",
    Category.COMPETITION: "Customers choose between {competitors|[named alternatives]} and doing nothing. We win when "
                          "[situation] because [differentiator with proof], and we lose when [situation]. Our win rate "
                          "against [main competitor] is [win rate] across [number of competitive deals].",
    Category.DEFENSIBILITY: "Our defensibility comes from {intellectual_property|[IP, data or network advantage]}. It "
                            "protects [what], but not [what], so we also rely on [switching costs or data advantage], "
                            "which compounds as [mechanism]. Replicating it would take a funded competitor [time and cost].",
    Category.TEAM: "We are positioned to win because [founder] spent [years] in [relevant experience] and [founder] "
                   "built [relevant achievement]. Our critical gap is [role], and we will hire for it by [date] using "
                   "[part of this round].",
    Category.FINANCIAL_PERFORMANCE: "Over the last twelve months revenue was {revenue|[revenue]}, gross margin was "
                                    "{gross_margin|[gross margin]} and monthly burn is {monthly_burn|[monthly burn]}. "
                                    "Burn moved from [prior burn] to [current burn] while revenue grew [growth rate], "
                                    "so our burn multiple is [burn multiple].",
    Category.FINANCIAL_PROJECTIONS: "Our projections rest on three assumptions: [new customers per quarter], [average "
                                    "contract value] and [churn]. Each is based on [evidence]. If growth comes in "
                                    "[shortfall] below plan, we [specific response] and still reach [milestone] within "
                                    "our runway.",
    Category.RISKS: "Our biggest risk is [risk]. We are mitigating it by [action], and we will know it is working when "
                    "[leading indicator] reaches [threshold] by [date]. The second risk is [risk], which we address "
                    "through [action].",
    Category.FUNDRAISING: "We are raising {raise_amount|[round size]} on {instrument|[instrument]}, with "
                          "{amount_committed|[amount committed]} committed and [lead investor status]. The round funds "
                          "{runway_months|[months of runway]} to reach [milestone], which sets up [next round] at "
                          "[target metrics].",
    Category.VALUATION: "Our {valuation|[valuation]} valuation is based on [comparable financings] and our current "
                        "{arr|[ARR]} growing {revenue_growth|[growth rate]}. At this price an investor needs an exit of "
                        "[exit value] to return [target multiple] on the round, consistent with [comparable exits].",
    Category.USE_OF_FUNDS: "We will allocate the round to {use_of_funds|[allocation by category]}. Each allocation is "
                           "tied to a milestone: [allocation] funds [milestone]. The round gives us "
                           "{runway_months|[months of runway]}, and if it came in smaller we would cut [item] first.",
    Category.MILESTONES: "Before our next raise we will hit {milestones|[three milestones]}. The milestone that matters "
                         "most to next-round investors is [milestone], and we are on track because [evidence]. If it "
                         "slips by six months, we [contingency].",
    Category.EXIT_POTENTIAL: "Logical acquirers are [acquirer types] because [strategic rationale]. Comparable "
                             "transactions include [comparable exits], and at [revenue scale] a company like ours could "
                             "be worth [exit value], which returns this round [target multiple].",
}

STRUCTURE_HINTS: dict[Category, str] = {
    Category.MARKET: "show the bottom-up math (customers × price) and name the source",
    Category.TRACTION: "give current figures, the trend over a stated period, and quality signals like retention",
    Category.UNIT_ECONOMICS: "state CAC and LTV with the formula and inputs, then payback in months",
    Category.FUNDRAISING: "state amount, instrument, committed capital and the milestone the round buys",
    Category.VALUATION: "anchor the valuation in comparables and traction, then the return math",
    Category.COMPETITION: "name real alternatives, when you win and when you lose",
    Category.FINANCIAL_PROJECTIONS: "name the few assumptions that drive the plan and the evidence for each",
    Category.RISKS: "name the risk, the mitigation and the signal that shows it is working",
}

_FILL = re.compile(r"\{(?P<field>[a-z_]+)\|(?P<placeholder>\[[^\]]+\])\}")


def fill_template(template: str, profile: CompanyProfile) -> str:
    """Use confirmed profile values (never INFERRED ones); otherwise keep the placeholder."""

    def substitute(match: re.Match[str]) -> str:
        fv = profile.get(match.group("field"))
        if fv is None or fv.status is DataStatus.INFERRED or not fv.display:
            return match.group("placeholder")
        return truncate(fv.display, 120)

    return _FILL.sub(substitute, template)


def _assessment(scores: DimensionScores) -> str:
    total = scores.total
    if total >= 16:
        return "An investor would hear a direct, well-supported answer that builds confidence in the founder's command of this topic."
    if total >= 12:
        return "An investor would find this answer credible but incomplete; they would want more specific proof before relying on it."
    if total >= 8:
        return "An investor would come away with doubts: the answer leaves key points unsupported or unaddressed."
    return "An investor would likely read this answer as a lack of command of the business on this topic, which would weigh against the deal."


def heuristic_coaching(category: Category, question: str, answer: str, scores: DimensionScores,
                       profile: CompanyProfile) -> Coaching:
    strengths: list[str] = []
    weaknesses: list[str] = []
    if scores.clarity >= 4:
        strengths.append("You answered directly and the main point was easy to follow.")
    if scores.evidence >= 4:
        strengths.append("You supported the answer with specific figures or proof points.")
    if scores.credibility >= 4:
        strengths.append("The answer sounded measured and consistent rather than promotional.")
    if scores.completeness >= 4:
        strengths.append("You covered the parts of the question that matter.")
    if scores.clarity <= 3:
        weaknesses.append("Lead with the direct answer in the first sentence; the point is currently buried or hedged.")
    if scores.evidence <= 3:
        weaknesses.append("Add specific metrics, customer evidence or sources; the answer relies on assertion.")
    if scores.credibility <= 3:
        weaknesses.append("Remove hedging and absolute claims, and state the assumptions behind your numbers.")
    if scores.completeness <= 3:
        weaknesses.append("Address every part of the question; investors notice the parts that are skipped.")
    if len(answer.split()) > 350:
        weaknesses.append("Tighten the answer: aim for 60-150 words in a live meeting.")

    hint = STRUCTURE_HINTS.get(category, "support the answer with two or three specific proof points")
    structure = (f"1) Give the direct answer in one sentence. 2) Support it: {hint}. "
                 "3) State the key assumption and how you are validating it. "
                 "4) Close with what it means for the business or this round.")
    return Coaching(
        investor_assessment=_assessment(scores),
        strengths=strengths,
        weaknesses=weaknesses,
        answer_structure=structure,
        model_answer=fill_template(MODEL_ANSWER_TEMPLATES[category], profile),
    )

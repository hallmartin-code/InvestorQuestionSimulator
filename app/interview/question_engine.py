"""Interview planning and question generation.

Planning is deterministic: each topic gets a priority from the company's stage plus the
weight of open findings and missing information on that topic. The highest-priority topics
are selected and ordered the way a real meeting runs (understanding -> evidence ->
investment committee). Difficulty rises with progress through the interview.

Question wording comes from the model, grounded in the deck and the topic's findings. Every
candidate is checked against questions already asked; near-duplicates are retried once and
then replaced with a profile-aware template question.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.analysis.evidence_mapper import category_digest, fields_for_category
from app.integrations.llm_client import StructuredLLM
from app.interview.scoring_engine import transcript_digest
from app.models.analysis import GapAnalysis, Issue
from app.models.company import CompanyProfile, DataStatus, stage_key
from app.models.interview import CATEGORY_PHASE, Category, Difficulty, InterviewState, Question, TopicPlan
from app.utils.text import jaccard, sequence_ratio, truncate

C = Category

DUPLICATE_JACCARD = 0.6
DUPLICATE_SEQUENCE = 0.85
MAX_QUESTION_CHARS = 700

_BASE_SEED = {
    C.PROBLEM: 2.0, C.CUSTOMER: 2.5, C.PRODUCT_SOLUTION: 2.0, C.MARKET: 2.5, C.BUSINESS_MODEL: 2.5, C.TEAM: 2.5,
    C.TRACTION: 3.0, C.UNIT_ECONOMICS: 2.0, C.GO_TO_MARKET: 2.5, C.COMPETITION: 2.0, C.DEFENSIBILITY: 1.5,
    C.FINANCIAL_PERFORMANCE: 1.0, C.FINANCIAL_PROJECTIONS: 1.5, C.RISKS: 2.0, C.FUNDRAISING: 3.0,
    C.VALUATION: 2.0, C.USE_OF_FUNDS: 2.5, C.MILESTONES: 2.0, C.EXIT_POTENTIAL: 1.0,
}
STAGE_BASE_PRIORITY: dict[str, dict[Category, float]] = {
    "pre_seed": _BASE_SEED | {C.PROBLEM: 3.0, C.CUSTOMER: 3.0, C.TEAM: 3.0, C.TRACTION: 1.5, C.UNIT_ECONOMICS: 0.5,
                              C.FINANCIAL_PERFORMANCE: 0.5, C.FINANCIAL_PROJECTIONS: 1.0, C.MILESTONES: 2.5},
    "seed": _BASE_SEED,
    "series_a": _BASE_SEED | {C.PROBLEM: 1.5, C.TRACTION: 3.5, C.UNIT_ECONOMICS: 3.0, C.GO_TO_MARKET: 3.0,
                              C.FINANCIAL_PERFORMANCE: 2.5, C.FINANCIAL_PROJECTIONS: 2.5, C.VALUATION: 2.5},
    "series_b_plus": _BASE_SEED | {C.PROBLEM: 1.0, C.PRODUCT_SOLUTION: 1.5, C.TRACTION: 3.5, C.UNIT_ECONOMICS: 3.5,
                                   C.GO_TO_MARKET: 3.0, C.FINANCIAL_PERFORMANCE: 3.0, C.FINANCIAL_PROJECTIONS: 3.0,
                                   C.VALUATION: 3.0, C.EXIT_POTENTIAL: 2.0},
    "unknown": _BASE_SEED,
}

DIFFICULTY_GUIDE: dict[Difficulty, str] = {
    Difficulty.UNDERSTANDING: "LEVEL 1 - UNDERSTANDING: verify the founder understands this part of the business "
                              "and can explain it plainly and specifically.",
    Difficulty.EVIDENCE: "LEVEL 2 - EVIDENCE: challenge the claims with numbers and proof; ask for the metrics, sources, "
                         "calculations and customer evidence behind them.",
    Difficulty.INVESTMENT_COMMITTEE: "LEVEL 3 - INVESTMENT COMMITTEE: test assumptions, risks, trade-offs, valuation, "
                                     "capital efficiency and investment return potential.",
}


# --- Planning ----------------------------------------------------------------------------

def topic_priority(category: Category, stage: str, issues: list[Issue], missing: int) -> tuple[float, str]:
    base = STAGE_BASE_PRIORITY.get(stage, _BASE_SEED)[category]
    issue_weight = sum(i.priority_weight for i in issues)
    missing_bonus = min(1.5, 0.5 * missing)
    top = max(issues, key=lambda i: i.priority_weight, default=None)
    reason = f"{top.tag.value}: {top.title}" if top else ("Missing information" if missing else "Core investor topic")
    return round(base + issue_weight + missing_bonus, 2), reason


def plan_interview(profile: CompanyProfile, gaps: GapAnalysis, max_questions: int) -> list[TopicPlan]:
    stage = stage_key(profile.display("stage"))
    scored: list[TopicPlan] = []
    for category in Category:
        issues = gaps.for_category(category)
        _present, missing = fields_for_category(profile, category)
        priority, reason = topic_priority(category, stage, issues, len(missing))
        scored.append(TopicPlan(category=category, priority=priority, issue_ids=[i.issue_id for i in issues],
                                reason=reason))
    # Leave roughly a quarter of the question budget for adaptive follow-ups.
    topic_count = max(3, min(len(scored), round(max_questions * 0.75)))
    selected = sorted(scored, key=lambda t: -t.priority)[:topic_count]
    if not any(t.category is C.FUNDRAISING for t in selected):
        fundraising = next(t for t in scored if t.category is C.FUNDRAISING)
        selected[-1] = fundraising
    return sorted(selected, key=lambda t: (CATEGORY_PHASE[t.category], -t.priority))


def difficulty_for(index: int, max_questions: int) -> Difficulty:
    """Level by position: first third understanding, middle third evidence, final third IC."""
    fraction = index / max(1, max_questions)
    if fraction < 1 / 3:
        return Difficulty.UNDERSTANDING
    if fraction < 2 / 3:
        return Difficulty.EVIDENCE
    return Difficulty.INVESTMENT_COMMITTEE


# --- Deduplication -------------------------------------------------------------------------

def is_near_duplicate(candidate: str, existing: str) -> bool:
    return jaccard(candidate, existing) >= DUPLICATE_JACCARD or sequence_ratio(candidate, existing) >= DUPLICATE_SEQUENCE


def is_duplicate(candidate: str, asked: list[Question]) -> bool:
    return any(is_near_duplicate(candidate, q.text) for q in asked)


def new_question(state: InterviewState, *, category: Category, text: str, difficulty: Difficulty,
                 source: str, intent: str = "", issue_ids: list[str] | None = None,
                 parent: Question | None = None) -> Question:
    count = sum(1 for q in state.asked if q.category is category)
    return Question(
        question_id=f"{category.value}_{count + 1:02d}", category=category, text=truncate(text, MAX_QUESTION_CHARS),
        difficulty=difficulty, is_follow_up=parent is not None,
        parent_question_id=parent.question_id if parent else None, issue_ids=issue_ids or [], intent=intent,
        source=source,  # type: ignore[arg-type]
    )


# --- Template questions ----------------------------------------------------------------------

def _fact(profile: CompanyProfile, name: str) -> str | None:
    fv = profile.get(name)
    if fv is None or fv.status is DataStatus.INFERRED:
        return None
    return truncate(fv.display, 90)


def _where(profile: CompanyProfile, name: str) -> str:
    fv = profile.get(name)
    return f" (slide {fv.page_or_slide})" if fv and fv.page_or_slide else ""


def template_questions(category: Category, profile: CompanyProfile) -> dict[Difficulty, list[str]]:
    """Candidate questions per level; profile-aware variants first, generic fallbacks after."""
    p, L1, L2, L3 = profile, Difficulty.UNDERSTANDING, Difficulty.EVIDENCE, Difficulty.INVESTMENT_COMMITTEE
    q: dict[Difficulty, list[str]] = {L1: [], L2: [], L3: []}
    tam, sam, som = _fact(p, "tam"), _fact(p, "sam"), _fact(p, "som")
    arr, revenue, growth = _fact(p, "arr"), _fact(p, "revenue"), _fact(p, "revenue_growth")

    if category is C.MARKET:
        if tam:
            q[L1].append(f"Your deck cites a {tam} TAM{_where(p, 'tam')}. Who exactly is the customer inside that number, "
                         "and how did you arrive at it?")
        if tam and som:
            q[L2].append(f"You show a {tam} TAM and a {som} SOM. Walk me through the calculation from one to the other: "
                         "which assumptions and sources drive each step?")
        elif tam:
            q[L2].append(f"Walk me through the bottom-up math behind your {tam} TAM: how many target customers, at what "
                         "annual price, and what portion is realistically reachable in the next five years?")
        q[L1].append("Who is the specific buyer you are targeting first, and how large is that segment in customers and dollars?")
        q[L2].append("Build your market size bottom-up for me: number of target customers, annual price, and the source for each input.")
        q[L3].append("If your serviceable market turns out to be half what you've modeled, is this still a venture-scale "
                     "company? What would have to be true to build a very large business here?")
    elif category is C.PROBLEM:
        q[L1].append("What specific problem are you solving, for whom, and what does it cost them today?")
        q[L2].append("What evidence shows customers feel this problem acutely enough to pay: interviews, lost revenue, "
                     "or budget already spent on workarounds?")
        q[L3].append("Why hasn't this problem already been solved by incumbents, and what changed that makes now the right time?")
    elif category is C.PRODUCT_SOLUTION:
        q[L1].append("Describe what your product does today, not the roadmap, and how a customer uses it in a typical week.")
        q[L2].append("What measurable outcome does your product deliver for customers, and how have you verified it?")
        q[L3].append("Which part of the product is genuinely hard to build, and what would it cost a well-funded "
                     "competitor to replicate it?")
    elif category is C.CUSTOMER:
        if (target := _fact(p, "target_customer")):
            q[L1].append(f"You're targeting {target}. Who is the buyer, who is the user, and who signs the contract?")
        q[L1].append("Who is the buyer, who is the user, and who signs the contract?")
        q[L2].append("Walk me through your last three customer wins: how they found you, how long it took, and why they bought.")
        q[L3].append("Which customer segment will you deliberately not pursue in the next 18 months, and why?")
    elif category is C.TRACTION:
        figure = arr or revenue
        if figure:
            q[L1].append(f"Your deck shows {figure}{_where(p, 'arr' if arr else 'revenue')}. Break that down for me: how "
                         "many customers, what's recurring versus one-time, and how has it trended over the last 12 months?")
        elif (customers := _fact(p, "customers") or _fact(p, "pilots")):
            q[L1].append(f"Your deck references {customers}. How many of those are paying, and what have they committed to?")
        q[L1].append("What traction can you point to today (paying customers, pilots, LOIs or usage), and what numbers support it?")
        if growth:
            q[L2].append(f"You cite {growth} growth{_where(p, 'revenue_growth')}. Over what period, from what base, and what drove it?")
        q[L2].append("What has your month-over-month growth been over the last six months, and what drove the best and worst months?")
        q[L3].append("If I called your three largest customers tomorrow, what would they say about renewal and expansion, "
                     "and what share of revenue do they represent?")
    elif category is C.BUSINESS_MODEL:
        if (pricing := _fact(p, "pricing")):
            q[L1].append(f"Your pricing is {pricing}. Who pays, how often, and how did you set that price?")
        q[L1].append("How exactly do you make money: who pays, how much, and how often?")
        if (margin := _fact(p, "gross_margin")):
            q[L2].append(f"You report {margin} gross margin. What's in cost of revenue, and how does that change at scale?")
        q[L2].append("What are your gross margins today and at scale, and what drives the difference?")
        q[L3].append("Which assumption in your business model, if wrong, would break the economics, and how are you testing it?")
    elif category is C.UNIT_ECONOMICS:
        q[L1].append("What does it cost you to acquire a customer, and how do you calculate that?")
        if (cac := _fact(p, "cac")) and (ltv := _fact(p, "ltv")):
            q[L2].append(f"You show CAC of {cac} and LTV of {ltv}. What inputs (churn, margin, contract length) go into "
                         "the LTV, and how many months does it take to recover CAC?")
        q[L2].append("What is your CAC payback period in months, and which sales and marketing costs are included?")
        q[L3].append("If CAC doubles as you move beyond early adopters, what happens to your capital needs and path to profitability?")
    elif category is C.GO_TO_MARKET:
        q[L1].append("Walk me through your current sales process from first contact to signed contract.")
        q[L2].append("What is your sales cycle length and conversion rate at each stage of the funnel, based on actual data?")
        q[L3].append("Which acquisition channel will produce most of your next hundred customers, and what evidence shows "
                     "it scales efficiently?")
    elif category is C.COMPETITION:
        if (competitors := _fact(p, "competitors")):
            q[L1].append(f"You list {competitors} as alternatives. Why do customers choose you over them, and when do you lose?")
        q[L1].append("Who do customers use today instead of you, including doing nothing, and why would they switch?")
        q[L2].append("Tell me about a deal you lost to a competitor or to the status quo. What did you learn?")
        q[L3].append("If the largest incumbent in your space launched a copy of your product next quarter, what would you "
                     "do, and why would you still win?")
    elif category is C.DEFENSIBILITY:
        if (ip := _fact(p, "intellectual_property")):
            q[L1].append(f"Your deck mentions: {ip}. What exactly does that protect, and what doesn't it protect?")
        q[L1].append("What makes this business defensible as it scales?")
        q[L2].append("What evidence shows your advantage compounds over time: data, network effects or switching costs?")
        q[L3].append("What prevents a competitor with ten times your funding from copying this in 18 months?")
    elif category is C.TEAM:
        q[L1].append("Why is this team uniquely positioned to win this market?")
        q[L2].append("Which critical role is missing from the team today, and what's your plan and timeline to fill it?")
        q[L3].append("What is the hardest disagreement the founding team has had, and how was it resolved?")
    elif category is C.FINANCIAL_PERFORMANCE:
        q[L1].append("What were your revenue, burn and gross margin over the last twelve months?")
        if (burn := _fact(p, "monthly_burn")):
            q[L2].append(f"Your monthly burn is {burn}. What does it buy, and how has it changed relative to revenue?")
        q[L2].append("What is your current monthly burn, and how has your burn multiple trended?")
        q[L3].append("Where have your actual results missed your own plan in the last year, and why?")
    elif category is C.FINANCIAL_PROJECTIONS:
        if (projection := _fact(p, "financial_projections")):
            q[L1].append(f"Your deck projects: {projection}. Walk me through how you built that forecast.")
        q[L1].append("Walk me through your revenue projections for the next three years at a high level.")
        q[L2].append("What are the three assumptions that drive most of your projected growth, and what evidence supports each?")
        q[L3].append("If your growth rate ends up 40% below plan, what changes in your financing strategy and how much "
                     "additional dilution would you expect before reaching breakeven?")
    elif category is C.RISKS:
        q[L1].append("What is the biggest risk to this business in the next 18 months?")
        q[L2].append("For your top two risks, what are you doing now to mitigate them, and how will you know it's working?")
        q[L3].append("What would cause you to conclude this business isn't working, and what would you do then?")
    elif category is C.FUNDRAISING:
        if (raise_amount := _fact(p, "raise_amount")):
            instrument = _fact(p, "instrument")
            suffix = f" on a {instrument}" if instrument else ""
            q[L1].append(f"You're raising {raise_amount}{suffix}. How much is committed, who is leading, and when do you "
                         "expect to close?")
        q[L1].append("How much are you raising, on what instrument, and how much is already committed?")
        q[L2].append("Why does this business require venture capital rather than revenue, grants or debt?")
        q[L3].append("If you only raise half of this round, what exactly changes in your plan and runway?")
    elif category is C.VALUATION:
        if (valuation := _fact(p, "valuation")):
            q[L1].append(f"What is the basis for your {valuation} valuation?")
        q[L1].append("What valuation or cap are you proposing, and how did you arrive at it?")
        q[L2].append("Which comparable financings support that valuation, and how does your traction compare?")
        q[L3].append("Why is this valuation justified today, and what exit would an investor at this price need to earn "
                     "a venture return?")
    elif category is C.USE_OF_FUNDS:
        if (uses := _fact(p, "use_of_funds")):
            q[L1].append(f"Your use of funds allocates to {uses}. What does each allocation deliver?")
        q[L1].append("How will you allocate this round, by major category and share?")
        q[L2].append("What specific milestone does this financing purchase, and how many months of runway does it provide?")
        q[L3].append("Which part of the use of funds would you cut first if the round came in smaller, and what would that cost you?")
    elif category is C.MILESTONES:
        if (milestones := _fact(p, "milestones")):
            q[L1].append(f"Your milestones include {milestones}. Which of these matters most before your next raise, and why?")
        q[L1].append("What are the three milestones you'll hit before your next raise?")
        q[L2].append("Which milestone will make the next round easy to raise, and what evidence says you can hit it on this runway?")
        q[L3].append("What happens if your key milestone slips by six months?")
    elif category is C.EXIT_POTENTIAL:
        q[L1].append("Who are the logical acquirers for this company, and why?")
        q[L2].append("What comparable exits support your view of exit value, and how does your business compare?")
        q[L3].append("What would the company need to look like at exit to return this round several times over, and how "
                     "realistic is that path?")
    return q


def template_question(category: Category, difficulty: Difficulty, profile: CompanyProfile,
                      asked: list[Question]) -> str:
    candidates = template_questions(category, profile)
    order = [difficulty] + [d for d in Difficulty if d is not difficulty]
    for level in order:
        for text in candidates[level]:
            if not is_duplicate(text, asked):
                return text
    return (f"What else about {category.label.lower()} should an investor understand that your deck doesn't show, "
            "and what evidence supports it?")


# --- Model-generated questions ---------------------------------------------------------------

class QuestionOutput(BaseModel):
    question: str = Field(description="The single question to ask, as you would say it in the meeting.")
    intent: str = Field(description="One sentence: what this question tests.")


QUESTION_INSTRUCTIONS = """Task: write your next interview question for the founder of {company}.

Topic: {topic}
Why this topic is on the agenda: {reason}
Difficulty: {difficulty}
This is question {number} of about {total}.

Company profile entries for this topic:
{digest}

Open findings for this topic:
{issues}

Interview so far:
{transcript}

Questions already asked (do not repeat or paraphrase any of them):
{asked}

Requirements:
- Ask exactly one question, as you would say it in the meeting. It may have up to three tightly connected parts.
- Anchor it in this company's specifics: cite figures, claims or slides from the deck where they exist, or name the specific missing information and ask for it.
- Match the difficulty level.
- Do not state facts that are not in the material. No preamble, numbering or coaching.{retry}"""


def _issues_text(issues: list[Issue]) -> str:
    if not issues:
        return "- none recorded"
    return "\n".join(f"- [{i.tag.value}] {i.title}: {i.detail}" + (f" Evidence: {'; '.join(i.evidence)}" if i.evidence else "")
                     for i in issues[:6])


def generate_question(*, llm: StructuredLLM | None, effort: str | None, context: str, profile: CompanyProfile,
                      gaps: GapAnalysis, state: InterviewState, topic: TopicPlan,
                      difficulty: Difficulty) -> tuple[Question, str | None]:
    """A grounded, non-duplicate question for ``topic``; returns (question, notice for the user or None)."""
    open_issues = [i for i in gaps.for_category(topic.category) if i.issue_id not in state.resolved_issue_ids]
    notice: str | None = None
    if llm is not None:
        retry = ""
        for _attempt in range(2):
            instructions = QUESTION_INSTRUCTIONS.format(
                company=profile.company_name, topic=topic.category.label, reason=topic.reason,
                difficulty=DIFFICULTY_GUIDE[difficulty], number=state.answered + 1, total=state.max_questions,
                digest=category_digest(profile, topic.category), issues=_issues_text(open_issues),
                transcript=transcript_digest(state), asked="\n".join(f"- {q.text}" for q in state.asked) or "- none",
                retry=retry,
            )
            output, error = llm.structured(label="generate_question", output_model=QuestionOutput,
                                           instructions=instructions, context=context, effort=effort)
            if output is None:
                notice = f"AI question generation was unavailable ({error}); a standard question was used."
                break
            text = " ".join(output.question.split())
            if len(text) >= 15 and not is_duplicate(text, state.asked):
                return new_question(state, category=topic.category, text=text, difficulty=difficulty, source="llm",
                                    intent=truncate(output.intent, 240), issue_ids=topic.issue_ids), None
            retry = ("\n- Your previous draft was too similar to a question already asked. Ask about a different "
                     "unresolved aspect of this topic.")
    text = template_question(topic.category, difficulty, profile, state.asked)
    return new_question(state, category=topic.category, text=text, difficulty=difficulty, source="template",
                        intent=f"Command of {topic.category.label.lower()}", issue_ids=topic.issue_ids), notice

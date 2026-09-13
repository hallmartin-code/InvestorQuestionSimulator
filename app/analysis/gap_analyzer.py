"""Evidence and gap analysis: deterministic rules plus grounded model review.

Rules catch what arithmetic and the profile can prove (missing fields for the stage,
TAM/SAM/SOM ordering, committed > raise, conflicting figures, short runway, implied
multiples). The model adds judgment (vague or optimistic claims, weak positioning), and
every quote it cites is checked against the page it names; unverifiable claims of
inconsistency or red flags are downgraded to questions.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.evidence_mapper import primary_category, profile_digest
from app.ingestion.models import DeckDocument
from app.integrations.llm_client import StructuredLLM
from app.models.analysis import GapAnalysis, Issue, IssueTag
from app.models.company import FIELD_SPECS, STAGE_LABELS, CompanyProfile, stage_key
from app.models.interview import Category
from app.utils.money import format_money
from app.utils.text import content_tokens, jaccard, overlap_ratio, truncate

C = Category

CORE_REQUIRED: dict[str, Category] = {
    "problem": C.PROBLEM, "target_customer": C.CUSTOMER, "business_model": C.BUSINESS_MODEL, "tam": C.MARKET,
    "competitors": C.COMPETITION, "team": C.TEAM, "raise_amount": C.FUNDRAISING, "use_of_funds": C.USE_OF_FUNDS,
    "milestones": C.MILESTONES,
}
SEED_REQUIRED: dict[str, Category] = {
    "customers|customer_count|pilots": C.TRACTION, "pricing": C.BUSINESS_MODEL, "instrument": C.FUNDRAISING,
    "runway_months": C.FUNDRAISING,
}
SERIES_A_REQUIRED: dict[str, Category] = {
    "revenue|arr|mrr": C.TRACTION, "revenue_growth": C.TRACTION, "gross_margin": C.UNIT_ECONOMICS,
    "cac": C.UNIT_ECONOMICS, "ltv": C.UNIT_ECONOMICS, "churn|retention": C.TRACTION, "valuation": C.VALUATION,
    "financial_projections": C.FINANCIAL_PROJECTIONS, "sales_cycle": C.GO_TO_MARKET,
}
GROWTH_REQUIRED: dict[str, Category] = {
    "historical_financials": C.FINANCIAL_PERFORMANCE, "monthly_burn": C.FINANCIAL_PERFORMANCE,
}


def required_fields(stage: str) -> dict[str, Category]:
    required = dict(CORE_REQUIRED)
    if stage in ("seed", "series_a", "series_b_plus", "unknown"):
        required |= SEED_REQUIRED
    if stage in ("series_a", "series_b_plus"):
        required |= SERIES_A_REQUIRED
    if stage == "series_b_plus":
        required |= GROWTH_REQUIRED
    return required


_NO_COMPETITION = re.compile(r"(?i)\bno (?:direct |real |existing )?(?:competitors?|competition)\b|\bno one else\b")
_VAGUE = re.compile(
    r"(?i)\b(first[- ]mover|only (?:solution|company|platform)|revolutionary|disruptive|game[- ]chang\w*|"
    r"massive (?:market|opportunity)|conservative (?:estimate|projection|assumption)s?|unicorn|"
    r"hockey[- ]stick|guaranteed)\b"
)


def _category_for_slide(title: str | None) -> Category:
    text = (title or "").lower()
    for pattern, category in ((r"compet|landscape|advantage|why we win", C.COMPETITION),
                              (r"market|opportunity|\btam\b", C.MARKET),
                              (r"traction|growth|customers|results", C.TRACTION),
                              (r"financ|projection|forecast", C.FINANCIAL_PROJECTIONS),
                              (r"team|founder", C.TEAM),
                              (r"business model|pricing|revenue model", C.BUSINESS_MODEL)):
        if re.search(pattern, text):
            return category
    return C.PRODUCT_SOLUTION


class _IssueBuilder:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.issues: list[Issue] = []

    def add(self, tag: IssueTag, category: Category, title: str, detail: str, *, severity: int = 2,
            evidence: list[str] | None = None, fields: list[str] | None = None, source: str = "rules") -> None:
        self.issues.append(Issue(
            issue_id=f"{self.prefix}{len(self.issues) + 1:02d}", tag=tag, category=category, title=title,
            detail=detail, severity=severity, evidence=evidence or [], fields=fields or [], source=source,
        ))


def _cite(profile: CompanyProfile, name: str) -> str:
    fv = profile.get(name)
    if fv is None:
        return ""
    quote = f': "{truncate(fv.evidence, 100)}"' if fv.evidence and fv.source == "deck" else ""
    return f"{fv.location}{quote}"


def rule_based_issues(doc: DeckDocument | None, profile: CompanyProfile) -> list[Issue]:
    b = _IssueBuilder("R")
    stage = stage_key(profile.display("stage"))
    stage_label = STAGE_LABELS[stage]

    # 1. Missing information expected at this stage, grouped per topic.
    missing_by_category: dict[Category, list[str]] = {}
    for key, category in required_fields(stage).items():
        options = key.split("|")
        if not profile.has(*options):
            missing_by_category.setdefault(category, []).append(options[0])
    for category, names in missing_by_category.items():
        labels = ", ".join(FIELD_SPECS[n].label for n in names)
        critical = category in (C.FUNDRAISING, C.USE_OF_FUNDS) or (
            category is C.TRACTION and stage in ("series_a", "series_b_plus"))
        b.add(IssueTag.EVIDENCE_GAP, category, f"Missing: {labels}",
              f"The deck does not state {labels}. Investors evaluating a {stage_label.lower()} company expect this.",
              severity=3 if critical else 2, fields=names)

    # 2. Market sizing.
    tam, sam, som = profile.value("tam"), profile.value("sam"), profile.value("som")
    if tam is not None and profile.get("market_source") is None:
        b.add(IssueTag.EVIDENCE_GAP, C.MARKET, "Market size is unsourced",
              f"The {format_money(tam)} TAM has no cited source or bottom-up calculation.",
              evidence=[_cite(profile, "tam")], fields=["tam", "market_source"])
    pairs = [("som", som, "sam", sam), ("sam", sam, "tam", tam), ("som", som, "tam", tam)]
    for small_name, small, big_name, big in pairs:
        if small is not None and big is not None and small > big:
            b.add(IssueTag.INCONSISTENCY, C.MARKET, f"{small_name.upper()} exceeds {big_name.upper()}",
                  f"{small_name.upper()} ({format_money(small)}) is larger than {big_name.upper()} ({format_money(big)}).",
                  severity=3, evidence=[_cite(profile, small_name), _cite(profile, big_name)],
                  fields=[small_name, big_name])
            break
    if som is not None and tam and som <= tam and som / tam > 0.10:
        b.add(IssueTag.QUESTION, C.MARKET, "Obtainable share looks optimistic",
              f"SOM is {som / tam:.0%} of TAM (CALCULATION); investors will ask how that share is reachable.",
              evidence=[_cite(profile, "som")], fields=["som", "tam"])

    # 3. Fundraising arithmetic.
    raise_amount, committed = profile.value("raise_amount"), profile.value("amount_committed")
    if raise_amount is not None and committed is not None:
        if committed > raise_amount:
            b.add(IssueTag.INCONSISTENCY, C.FUNDRAISING, "Commitments exceed the round size",
                  f"{format_money(committed)} committed against a {format_money(raise_amount)} raise.",
                  severity=3, evidence=[_cite(profile, "amount_committed"), _cite(profile, "raise_amount")],
                  fields=["amount_committed", "raise_amount"])
        elif committed > 0:
            b.add(IssueTag.STRENGTH, C.FUNDRAISING, "Round has commitments",
                  f"{format_money(committed)} of {format_money(raise_amount)} is committed "
                  f"({committed / raise_amount:.0%}, CALCULATION).", severity=1,
                  evidence=[_cite(profile, "amount_committed")], fields=["amount_committed"])
    runway = profile.value("runway_months")
    if runway is not None:
        if runway < 6:
            b.add(IssueTag.RED_FLAG, C.FUNDRAISING, "Runway under six months",
                  f"Stated runway is {runway:g} months, which leaves little time to close a round.", severity=3,
                  evidence=[_cite(profile, "runway_months")], fields=["runway_months"])
        elif runway < 12:
            b.add(IssueTag.QUESTION, C.MILESTONES, "Runway may not reach the next milestone",
                  f"{runway:g} months of runway; investors will test whether milestones fit inside it.",
                  evidence=[_cite(profile, "runway_months")], fields=["runway_months", "milestones"])

    # 4. Conflicting figures across slides.
    for name, alternates in profile.alternates.items():
        spec = FIELD_SPECS[name]
        current = profile.get(name)
        if current is None:
            continue
        others = ", ".join(f"{a.display} ({a.location})" for a in alternates[:3])
        b.add(IssueTag.INCONSISTENCY, primary_category(name), f"Conflicting {spec.label} figures",
              f"{spec.label} is stated as {current.display} ({current.location}) and also as {others}.",
              severity=2, evidence=[_cite(profile, name), *(a.location for a in alternates[:3])], fields=[name])
    arr, mrr = profile.get("arr"), profile.get("mrr")
    if arr and mrr and arr.status.value != "CALCULATED" and arr.value and mrr.value:
        implied = float(mrr.value) * 12
        if abs(implied - float(arr.value)) > 0.2 * max(implied, float(arr.value)):
            b.add(IssueTag.INCONSISTENCY, C.TRACTION, "ARR and MRR do not reconcile",
                  f"MRR {mrr.display} implies {format_money(implied)} ARR (CALCULATION), but ARR is stated as {arr.display}.",
                  evidence=[_cite(profile, "arr"), _cite(profile, "mrr")], fields=["arr", "mrr"])

    # 5. Economics and growth sanity checks.
    growth = profile.value("revenue_growth")
    if growth is not None and growth > 300:
        b.add(IssueTag.QUESTION, C.TRACTION, "Growth rate needs context",
              f"{growth:g}% growth; investors will ask for the period, the starting base and what drove it.",
              evidence=[_cite(profile, "revenue_growth")], fields=["revenue_growth"])
    ratio = profile.value("ltv_cac_ratio")
    if ratio is not None:
        if ratio < 1:
            b.add(IssueTag.RED_FLAG, C.UNIT_ECONOMICS, "LTV below CAC",
                  f"LTV:CAC is {ratio:.1f}x (CALCULATION): each customer costs more to acquire than it returns.",
                  severity=3, fields=["ltv", "cac"])
        elif ratio > 10:
            b.add(IssueTag.QUESTION, C.UNIT_ECONOMICS, "LTV:CAC looks unusually high",
                  f"LTV:CAC of {ratio:.1f}x (CALCULATION) usually means CAC excludes costs or LTV assumes low churn.",
                  fields=["ltv", "cac"])
    margin = profile.value("gross_margin")
    model_text = (profile.display("business_model") + " " + profile.display("pricing")).lower()
    if margin is not None and margin < 50 and re.search(r"saas|software|subscription", model_text):
        b.add(IssueTag.QUESTION, C.BUSINESS_MODEL, "Gross margin is low for software",
              f"{margin:g}% gross margin for a software model; investors will ask what drives cost of revenue.",
              evidence=[_cite(profile, "gross_margin")], fields=["gross_margin"])
    valuation = profile.value("valuation")
    revenue_base = profile.value("arr") or profile.value("revenue")
    if valuation is not None and revenue_base:
        multiple = float(valuation) / float(revenue_base)
        if multiple > 40:
            b.add(IssueTag.QUESTION, C.VALUATION, "Valuation is a high multiple of revenue",
                  f"{format_money(valuation)} valuation is {multiple:.0f}x revenue (CALCULATION).",
                  evidence=[_cite(profile, "valuation")], fields=["valuation", "revenue", "arr"])
    elif valuation is not None and stage in ("series_a", "series_b_plus"):
        b.add(IssueTag.QUESTION, C.VALUATION, "Valuation without stated revenue",
              f"A {format_money(valuation)} valuation at {stage_label} with no revenue figure in the deck.",
              evidence=[_cite(profile, "valuation")], fields=["valuation"])
    if profile.get("financial_projections") and not profile.has("historical_financials", "revenue", "arr"):
        b.add(IssueTag.EVIDENCE_GAP, C.FINANCIAL_PROJECTIONS, "Projections without a historical base",
              "The deck projects financials but shows no achieved results to anchor them.",
              evidence=[_cite(profile, "financial_projections")], fields=["financial_projections"])

    # 6. Language that invites scrutiny.
    if doc is not None:
        for page in doc.pages:
            text = doc.page_text(page.number)
            if (m := _NO_COMPETITION.search(text)) is not None:
                b.add(IssueTag.RED_FLAG, C.COMPETITION, "Claims no competition",
                      "Claiming no competitors usually signals a misread market or no market; investors will probe "
                      "alternatives, including the status quo.", severity=3,
                      evidence=[f'Slide {page.number}: "{m.group(0)}"'])
                break
        vague = 0
        for page in doc.pages:
            for m in _VAGUE.finditer(doc.page_text(page.number)):
                if vague >= 3:
                    break
                vague += 1
                b.add(IssueTag.QUESTION, _category_for_slide(page.title),
                      f"Unsupported claim: “{m.group(0)}”",
                      "Superlatives without evidence reduce credibility; expect to be asked to substantiate it.",
                      severity=1, evidence=[f'Slide {page.number}: "{m.group(0)}"'])

    # 7. Strengths worth verifying.
    if profile.has("revenue", "arr", "mrr"):
        figure = profile.get("arr") or profile.get("revenue") or profile.get("mrr")
        b.add(IssueTag.STRENGTH, C.TRACTION, "Revenue traction stated",
              f"The deck states {figure.label} of {figure.display}.", severity=1,
              evidence=[_cite(profile, figure.field)], fields=[figure.field])
    ip = profile.display("intellectual_property").lower()
    if re.search(r"patent", ip) and not re.search(r"pending|provisional|plan", ip):
        b.add(IssueTag.STRENGTH, C.DEFENSIBILITY, "Patent protection stated", "The deck references patents.",
              severity=1, evidence=[_cite(profile, "intellectual_property")], fields=["intellectual_property"])
    retention = profile.value("retention")
    if retention is not None and retention >= 90:
        b.add(IssueTag.STRENGTH, C.TRACTION, "High retention stated", f"Retention of {retention:g}%.", severity=1,
              evidence=[_cite(profile, "retention")], fields=["retention"])

    return b.issues


# --- Model review ------------------------------------------------------------------------

class LLMEvidence(BaseModel):
    page: int = Field(description="Slide/page number.")
    quote: str = Field(description="Verbatim quote under 30 words from that slide.")


class LLMIssue(BaseModel):
    tag: Literal["STRENGTH", "QUESTION", "EVIDENCE_GAP", "INCONSISTENCY", "RED_FLAG"]
    category: Literal[tuple(c.value for c in Category)]  # type: ignore[valid-type]
    severity: int = Field(description="3 = could decide the investment, 2 = material, 1 = minor.")
    title: str = Field(description="Under 10 words.")
    detail: str = Field(description="One or two sentences.")
    evidence: list[LLMEvidence] = Field(description="Quotes supporting the finding; empty for absent information.")


class GapOutput(BaseModel):
    issues: list[LLMIssue]


GAP_INSTRUCTIONS = """Task: review this company as a skeptical investor preparing for a first meeting, and list the findings that should shape the interview.

Finding types:
- EVIDENCE_GAP: important information that is missing, or a claim with no support (for example, traction without numbers).
- INCONSISTENCY: figures or statements that contradict each other; cite both places.
- RED_FLAG: something that would make many investors pass or pause.
- QUESTION: a vague claim, unusually optimistic assumption, unclear metric or weak competitive position worth probing.
- STRENGTH: a genuinely strong, well-evidenced point an investor would still verify.

Rules:
- Every finding based on something the deck states must cite the slide and a verbatim quote under 30 words. Quotes are checked automatically against the slide text. Findings about absent information need no quote.
- Return at most 12 findings, most important first. Do not repeat the rule-based findings listed below.
- Use the category that the interview should probe: {categories}.

Company profile (with provenance):
{profile}

Rule-based findings already recorded:
{rule_findings}"""


def _same_finding(a: str, b: str) -> bool:
    """Similar titles, or one title's terms contained in the other's ("Missing: CAC" vs "Missing: CAC, LTV")."""
    if jaccard(a, b) >= 0.5:
        return True
    ta, tb = content_tokens(a), content_tokens(b)
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return bool(small) and len(small & large) / len(small) >= 0.8


def _ground_llm_issues(doc: DeckDocument | None, output: GapOutput, existing: list[Issue]) -> list[Issue]:
    b = _IssueBuilder("L")
    for item in output.issues[:12]:
        try:
            category = Category(item.category)
            tag = IssueTag(item.tag)
        except ValueError:
            continue
        verified: list[str] = []
        for ev in item.evidence:
            if doc is not None and 1 <= ev.page <= doc.page_count and \
                    overlap_ratio(ev.quote, doc.page_text(ev.page)) >= 0.6:
                verified.append(f'Slide {ev.page}: "{truncate(ev.quote, 120)}"')
        if tag in (IssueTag.INCONSISTENCY, IssueTag.RED_FLAG, IssueTag.STRENGTH) and item.evidence and not verified:
            tag = IssueTag.QUESTION  # the cited support could not be found; keep it as something to ask
        title = truncate(item.title, 90)
        if any(i.category == category and _same_finding(i.title, title) for i in existing + b.issues):
            continue
        b.add(tag, category, title, truncate(item.detail, 300), severity=max(1, min(3, item.severity)),
              evidence=verified, source="llm")
    return b.issues


def analyze_gaps(doc: DeckDocument | None, profile: CompanyProfile, *, context: str,
                 llm: StructuredLLM | None, effort: str | None = None) -> GapAnalysis:
    issues = rule_based_issues(doc, profile)
    warnings: list[str] = []
    llm_used = False
    if llm is not None:
        rule_text = "\n".join(f"- [{i.tag.value}] {i.category.value}: {i.title}" for i in issues) or "- none"
        instructions = GAP_INSTRUCTIONS.format(categories=", ".join(c.value for c in Category),
                                               profile=profile_digest(profile), rule_findings=rule_text)
        output, error = llm.structured(label="gap_analysis", output_model=GapOutput, instructions=instructions,
                                       context=context, effort=effort)
        if output is None:
            warnings.append(f"AI gap analysis was unavailable ({error}); rule-based findings were used.")
        else:
            llm_used = True
            issues.extend(_ground_llm_issues(doc, output, issues))
    return GapAnalysis(issues=issues, missing_fields=profile.missing_fields(), warnings=warnings, llm_used=llm_used)

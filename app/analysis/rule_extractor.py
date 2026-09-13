"""Deterministic, rule-based extraction from deck text.

Runs on every deck and is the only extractor in heuristic mode. It is deliberately
conservative: a figure is recorded only when a keyword and the figure appear together on a
line (or on an adjacent label line), with the slide and the line kept as evidence. Projected
or targeted figures are never recorded as achieved results.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.ingestion.models import DeckDocument, DeckPage
from app.models.company import CompanyProfile, DataStatus, FieldValue
from app.utils.money import PERCENT_RE, money_mentions
from app.utils.text import truncate

RULE_CONFIDENCE = 0.6
TITLE_CONFIDENCE = 0.5
KEYWORD_CONFIDENCE = 0.4

_PROJECTION = re.compile(
    r"(?i)\b(projected|projection|forecast|target(?:ed|ing)?|plan(?:ned)?|expected?|goal|will (?:reach|hit|generate)|"
    r"pro ?forma|by (?:end of )?20\d\d|outlook|budget(?:ed)?|reach(?:ing)?|grow(?:ing)? to|scale to|expand to|"
    r"aim(?:ing)? (?:to|for))\b"
)
#: Slides describing plans: their figures are never achieved results.
_PLAN_TITLE = re.compile(r"(?i)milestones?|roadmap|projections?|forecast|financial plan|use of (?:funds|proceeds)|timeline|next \d+")
_PRICING = re.compile(
    r"(?i)[$€£]\s?\d[\d,.]*\s?[km]?\s*(?:/|per)\s*(?:hospital|user|seat|month|year|site|customer|licen[sc]e|clinic|"
    r"location|provider|bed|unit|school|student|annum|yr|mo|facility|account)\b"
)
_YEAR = re.compile(r"\b(20\d\d)\b")
_SEPARATOR = re.compile(r"[;|•]|\.\s")
_SOURCE = re.compile(r"(?i)\b(?:source[sd]?|according to)\s*[:\-]?\s*([^\n]{3,140})")

MONEY_TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("amount_committed", re.compile(r"(?i)\b(committed|soft[- ]circled|commitments?|already secured|closed to date)\b")),
    ("prior_funding", re.compile(r"(?i)\b(raised to date|previously raised|have raised|total raised|prior funding|funding to date)\b")),
    ("raise_amount", re.compile(r"(?i)\b(raising|seeking|the ask|our ask|funding ask|round size|investment ask|capital raise|raise of|to raise|current round)\b")),
    ("valuation", re.compile(r"(?i)\b(valuation|pre[- ]money|post[- ]money|valuation cap)\b|(?<!market )\bcap\b")),
    ("tam", re.compile(r"\bTAM\b|(?i:total addressable)")),
    ("sam", re.compile(r"\bSAM\b|(?i:serviceable (?:addressable|available))")),
    ("som", re.compile(r"\bSOM\b|(?i:serviceable obtainable|obtainable market)")),
    ("arr", re.compile(r"\bARR\b|(?i:annual recurring revenue|annuali[sz]ed run[- ]rate|run[- ]rate)")),
    ("mrr", re.compile(r"\bMRR\b|(?i:monthly recurring revenue)")),
    ("cac", re.compile(r"\bCAC\b|(?i:customer acquisition cost|cost to acquire)")),
    ("ltv", re.compile(r"\b(?:LTV|CLV)\b|(?i:lifetime value)")),
    ("monthly_burn", re.compile(r"(?i)\b(monthly burn|burn rate|net burn|burn)\b")),
    ("revenue", re.compile(r"(?i)\b(revenue|sales|bookings)\b")),
)
ACHIEVED_ONLY = frozenset({"revenue", "arr", "mrr"})

PERCENT_TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gross_margin", re.compile(r"(?i)gross margins?")),
    ("churn", re.compile(r"(?i)\bchurn")),
    ("retention", re.compile(r"(?i)\bretention|\bNRR\b|\bNDR\b|renewal rate")),
    ("revenue_growth", re.compile(r"(?i)\b(yoy|year[- ]over[- ]year|mom|month[- ]over[- ]month|growth|grew|growing)\b")),
)
_GROWTH_EXCLUDE = re.compile(r"(?i)\b(cagr|market|industry|sector)\b")

TITLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("problem", re.compile(r"(?i)\b(problem|pain points?|the challenge)\b")),
    ("solution", re.compile(r"(?i)\bsolution\b")),
    ("product", re.compile(r"(?i)\b(product|platform|how it works|technology)\b")),
    ("business_model", re.compile(r"(?i)business model|revenue model|how we make money|monetization")),
    ("pricing", re.compile(r"(?i)\bpricing\b")),
    ("team", re.compile(r"(?i)\b(team|leadership|founders|management)\b")),
    ("differentiation", re.compile(r"(?i)competitive advantage|differentiat|why we win|\bmoat\b")),
    ("competitors", re.compile(r"(?i)\b(competition|competitors|competitive landscape)\b")),
    ("use_of_funds", re.compile(r"(?i)use of (funds|proceeds)|allocation of funds")),
    ("milestones", re.compile(r"(?i)\b(milestones|roadmap)\b")),
    ("exit_strategy", re.compile(r"(?i)\bexit\b|acquirers?|liquidity")),
    ("financial_projections", re.compile(r"(?i)projections?|forecast|financial plan|pro ?forma")),
    ("target_customer", re.compile(r"(?i)target customer|customer segment|who we serve|ideal customer")),
    ("market_size", re.compile(r"(?i)market (size|opportunity)|addressable market")),
    ("traction_text", re.compile(r"(?i)\btraction\b")),
)
LIST_FIELDS = frozenset({"use_of_funds", "milestones", "competitors"})
_COMPETITOR_NOISE = re.compile(
    r"(?i)\b(competition|competitive|landscape|us\b|our\b|we\b|price|pricing|feature|yes|no|legacy|manual|"
    r"high|low|cost|speed|accuracy|integration|scalab|ease)\b|[✓✔✗×]"
)

SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Healthcare": ("hospital", "clinic", "patient", "health system", "clinical", "physician", "care team"),
    "Biotech": ("therapeutic", "preclinical", "clinical trial", "biotech", "molecule", "drug candidate"),
    "Medical Devices": ("medical device", "510(k)", "medtech", "implant", "diagnostic device"),
    "Fintech": ("payments", "banking", "lending", "fintech", "credit", "underwriting"),
    "Enterprise Software": ("saas", "workflow", "enterprise software", "b2b software", "api"),
    "Climate & Energy": ("carbon", "emissions", "solar", "energy storage", "climate", "decarbon"),
    "Education": ("students", "edtech", "school district", "learning outcomes", "teachers"),
    "Consumer": ("consumers", "d2c", "direct-to-consumer", "shoppers", "e-commerce"),
    "Logistics": ("freight", "logistics", "shipping", "supply chain", "warehouse"),
    "Real Estate": ("proptech", "real estate", "property managers", "tenants"),
    "Cybersecurity": ("cybersecurity", "threat detection", "breach", "zero trust"),
    "AI & Data": ("machine learning", "artificial intelligence", "data platform", "large language model"),
}

BUSINESS_MODEL_KEYWORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Subscription (SaaS)", re.compile(r"(?i)\b(saas|subscription|per seat|annual contract|per user per month)\b")),
    ("Marketplace (take rate)", re.compile(r"(?i)\b(marketplace|take rate|commission)\b")),
    ("Transaction fees", re.compile(r"(?i)\b(transaction fee|per transaction|interchange)\b")),
    ("Licensing", re.compile(r"(?i)\b(licens(?:e|ing) fee|royalt(?:y|ies))\b")),
)


def _deck_value(field: str, value: object, page: int, evidence: str, confidence: float,
                note: str | None = None, status: DataStatus = DataStatus.VERIFIED_FROM_DECK) -> FieldValue:
    return FieldValue(field=field, value=value, status=status, source="deck" if status is DataStatus.VERIFIED_FROM_DECK
                      else "rule_inference", page_or_slide=page, evidence=truncate(evidence, 200),
                      confidence=confidence, note=note)


def _is_projection(line: str) -> tuple[bool, bool]:
    """(clearly a projection, ambiguous current-year figure)."""
    if _PROJECTION.search(line):
        return True, False
    years = [int(y) for y in _YEAR.findall(line)]
    current = datetime.now().year
    if any(y > current for y in years):
        return True, False
    return False, any(y == current for y in years)


def _nearest_trigger(line: str, start: int, end: int,
                     triggers: tuple[tuple[str, re.Pattern[str]], ...]) -> str | None:
    best: tuple[int, int, str] | None = None
    for order, (field, pattern) in enumerate(triggers):
        for match in pattern.finditer(line):
            if match.end() <= start:
                distance = start - match.end()
                if _SEPARATOR.search(line[match.end():start]):
                    distance += 40
            elif match.start() >= end:
                distance = match.start() - end + 5
                if _SEPARATOR.search(line[end:match.start()]):
                    distance += 40
            else:
                distance = 0
            if distance <= 60 and (best is None or (distance, order) < best[:2]):
                best = (distance, order, field)
    return best[2] if best else None


def _has_trigger(line: str, triggers: tuple[tuple[str, re.Pattern[str]], ...]) -> str | None:
    for field, pattern in triggers:
        if pattern.search(line):
            return field
    return None


def _extract_money(page: DeckPage, profile: CompanyProfile, *, plan_slide: bool = False) -> None:
    lines = page.lines()
    consumed: set[int] = set()
    for index, line in enumerate(lines):
        for text, value, offset in money_mentions(line):
            field = _nearest_trigger(line, offset, offset + len(text), MONEY_TRIGGERS)
            context = line
            if field is None:
                for neighbour in (index - 1, index + 1):
                    if 0 <= neighbour < len(lines) and neighbour not in consumed \
                            and not money_mentions(lines[neighbour]):
                        field = _has_trigger(lines[neighbour], MONEY_TRIGGERS)
                        if field:
                            consumed.add(neighbour)
                            context = f"{lines[neighbour]} {line}" if neighbour < index else f"{line} {lines[neighbour]}"
                            break
            if field is None:
                continue
            projection, ambiguous = _is_projection(context)
            projection = projection or plan_slide
            if field in ACHIEVED_ONLY and projection:
                if profile.get("financial_projections") is None:
                    profile.set(_deck_value("financial_projections", truncate(context, 300), page.number, context,
                                            TITLE_CONFIDENCE))
                continue
            note = "Figure sits next to the current year; confirm it is achieved, not forecast." if (
                field in ACHIEVED_ONLY and ambiguous) else None
            confidence = TITLE_CONFIDENCE if note else RULE_CONFIDENCE
            if field == "monthly_burn" and re.search(r"(?i)annual|per year|/yr", context):
                value = value / 12
                note = "Converted from an annual burn figure."
            profile.set(_deck_value(field, value, page.number, context, confidence, note))


def _extract_percentages(page: DeckPage, profile: CompanyProfile, *, plan_slide: bool = False) -> None:
    for line in page.lines():
        for match in PERCENT_RE.finditer(line):
            field = _nearest_trigger(line, match.start(), match.end(), PERCENT_TRIGGERS)
            if field is None:
                continue
            if field == "revenue_growth" and _GROWTH_EXCLUDE.search(line):
                continue
            if plan_slide or (field == "revenue_growth" and _is_projection(line)[0]):
                continue
            profile.set(_deck_value(field, float(match.group(1)), page.number, line, RULE_CONFIDENCE))


_RUNWAY = re.compile(r"(?i)(\d{1,2})\s*\+?\s*months?\s+(?:of\s+)?runway|runway\s*(?:of|to|:|-)?\s*(\d{1,2})\s*months?")
_FOUNDED = re.compile(r"(?i)\b(?:founded|established|est\.|incorporated)\s*(?:in\s*)?((?:19|20)\d{2})\b")
_CUSTOMER_COUNT = re.compile(
    r"(?i)\b(\d[\d,]*)\+?\s+(?:paying\s+|active\s+|enterprise\s+)?(customers|clients|hospitals|health systems|"
    r"clinics|schools|brands|retailers|enterprises|accounts|logos)\b"
)
_PILOT = re.compile(r"(?i)\bpilots?\b")
_CONTRACT = re.compile(r"(?i)\b(signed contracts?|contracts?|LOIs?|letters? of intent|purchase orders?|MSAs?)\b")
_STAGE = re.compile(r"(?i)\b(pre[- ]seed|seed|series\s+[a-e])\b")
_ROUND_CONTEXT = re.compile(r"(?i)\b(round|raising|raise|funding|financing|investment)\b")
_INSTRUMENT = re.compile(r"\bSAFE\b|(?i:convertible notes?|priced (?:equity )?round|preferred (?:stock|equity)|equity round)")
_GEOGRAPHY = re.compile(r"(?:headquartered|based|HQ)\s+(?:in|:)\s*([A-Z][A-Za-z.\- ]+(?:,\s*[A-Z][A-Za-z.\- ]+)?)")
_IP = re.compile(r"(?i)\b(patents?|patent[- ]pending|provisional patent|trade secrets?)\b")
_REGULATORY = re.compile(r"\bFDA\b|510\(k\)|\bCE mark|\bHIPAA\b|\bSOC ?2\b|(?i:\bregulatory\b|\bclearance\b)")
_SALES_CYCLE = re.compile(r"(?i)\bsales cycles?\b")


def _extract_patterns(page: DeckPage, profile: CompanyProfile, *, plan_slide: bool = False) -> None:
    for line in page.lines():
        if (m := _RUNWAY.search(line)) is not None:
            profile.set(_deck_value("runway_months", float(m.group(1) or m.group(2)), page.number, line, RULE_CONFIDENCE))
        if (m := _FOUNDED.search(line)) is not None:
            profile.set(_deck_value("founding_year", int(m.group(1)), page.number, line, RULE_CONFIDENCE))
        if _PRICING.search(line) and profile.get("pricing") is None and not plan_slide:
            profile.set(_deck_value("pricing", truncate(line, 200), page.number, line, RULE_CONFIDENCE))
        if (m := _CUSTOMER_COUNT.search(line)) is not None and not plan_slide and not _is_projection(line)[0]:
            count = float(m.group(1).replace(",", ""))
            profile.set(_deck_value("customer_count", count, page.number, line, RULE_CONFIDENCE))
            if profile.get("customers") is None:
                profile.set(_deck_value("customers", truncate(line, 200), page.number, line, RULE_CONFIDENCE))
        if _PILOT.search(line) and profile.get("pilots") is None:
            profile.set(_deck_value("pilots", truncate(line, 200), page.number, line, TITLE_CONFIDENCE))
        if _CONTRACT.search(line) and profile.get("contracts") is None and not plan_slide \
                and not _is_projection(line)[0]:
            profile.set(_deck_value("contracts", truncate(line, 200), page.number, line, TITLE_CONFIDENCE))
        if (m := _STAGE.search(line)) is not None and _ROUND_CONTEXT.search(line) and profile.get("stage") is None:
            label = " ".join(w.capitalize() if w.lower() != "a" else "A" for w in re.split(r"[\s-]+", m.group(1)))
            label = label.replace("Pre Seed", "Pre-seed").replace("Series A", "Series A")
            profile.set(_deck_value("stage", label, page.number, line, RULE_CONFIDENCE))
        if (m := _INSTRUMENT.search(line)) is not None and profile.get("instrument") is None:
            profile.set(_deck_value("instrument", m.group(0), page.number, line, RULE_CONFIDENCE))
        if (m := _GEOGRAPHY.search(line)) is not None and profile.get("geography") is None:
            profile.set(_deck_value("geography", m.group(1).strip(" ."), page.number, line, RULE_CONFIDENCE))
        if _IP.search(line) and profile.get("intellectual_property") is None:
            profile.set(_deck_value("intellectual_property", truncate(line, 200), page.number, line, TITLE_CONFIDENCE))
        if _REGULATORY.search(line) and profile.get("regulatory_status") is None:
            profile.set(_deck_value("regulatory_status", truncate(line, 200), page.number, line, TITLE_CONFIDENCE))
        if _SALES_CYCLE.search(line) and profile.get("sales_cycle") is None:
            profile.set(_deck_value("sales_cycle", truncate(line, 160), page.number, line, TITLE_CONFIDENCE))
        if (m := _SOURCE.search(line)) is not None and profile.get("market_source") is None and (
                re.search(r"\b(TAM|SAM|SOM)\b|(?i:market)", line) or re.search(r"(?i)market", page.title or "")):
            profile.set(_deck_value("market_source", m.group(1).strip(" )."), page.number, line, RULE_CONFIDENCE))


def _body_lines(page: DeckPage) -> list[str]:
    lines = page.lines()
    if lines and page.title and lines[0].strip() == page.title.strip():
        lines = lines[1:]
    return lines


def _extract_by_title(page: DeckPage, profile: CompanyProfile) -> None:
    title = page.title or ""
    for field, pattern in TITLE_RULES:
        if not pattern.search(title):
            continue
        body = _body_lines(page)
        if sum(len(b) for b in body) < 15:
            continue
        if field == "traction_text":
            continue  # figures on traction slides are handled by the money/percent rules
        if field in LIST_FIELDS:
            items = [truncate(b, 90) for b in body if 2 < len(b) <= 90]
            if field == "competitors":
                items = [b for b in items if len(b) <= 40 and not re.search(r"\d", b) and not _COMPETITOR_NOISE.search(b)]
                confidence, note = KEYWORD_CONFIDENCE, "Heuristic list from the competition slide; review."
            else:
                confidence, note = TITLE_CONFIDENCE, None
            if items and profile.get(field) is None:
                profile.set(_deck_value(field, items[:8], page.number, "; ".join(items[:4]), confidence, note))
            continue
        if profile.get(field) is None:
            text = truncate("; ".join(body), 400)
            profile.set(_deck_value(field, text, page.number, " ".join(body), TITLE_CONFIDENCE))
        break


def _extract_identity(doc: DeckDocument, profile: CompanyProfile) -> None:
    first = doc.pages[0] if doc.pages else None
    if first is None:
        return
    lines = first.lines()
    generic = re.compile(r"(?i)confidential|pitch deck|investor (presentation|deck)|presentation|^page \d")
    if lines and 2 <= len(lines[0]) <= 60 and not generic.search(lines[0]):
        profile.set(_deck_value("company_name", lines[0], 1, lines[0], 0.55))
        if len(lines) > 1 and 15 <= len(lines[1]) <= 200 and not generic.search(lines[1]):
            profile.set(_deck_value("company_description", lines[1], 1, lines[1], 0.45))


def _infer_categoricals(doc: DeckDocument, profile: CompanyProfile) -> None:
    text = doc.full_text().lower()
    if profile.get("sector") is None:
        hits = {sector: sum(text.count(k) for k in keywords) for sector, keywords in SECTOR_KEYWORDS.items()}
        sector, count = max(hits.items(), key=lambda kv: kv[1])
        if count >= 3:
            matched = ", ".join(k for k in SECTOR_KEYWORDS[sector] if k in text)
            profile.set(FieldValue(field="sector", value=sector, status=DataStatus.INFERRED, source="rule_inference",
                                   evidence=f"Keyword matches: {matched}", confidence=KEYWORD_CONFIDENCE,
                                   note="Inferred from deck vocabulary; confirm."))
    if profile.get("business_model") is None:
        full = doc.full_text()
        for label, pattern in BUSINESS_MODEL_KEYWORDS:
            if (m := pattern.search(full)) is not None:
                profile.set(FieldValue(field="business_model", value=label, status=DataStatus.INFERRED,
                                       source="rule_inference", evidence=f"Deck mentions '{m.group(0)}'",
                                       confidence=KEYWORD_CONFIDENCE, note="Inferred from deck vocabulary; confirm."))
                break


def extract_rule_based(doc: DeckDocument, profile: CompanyProfile | None = None) -> CompanyProfile:
    profile = profile or CompanyProfile()
    _extract_identity(doc, profile)
    for page in doc.pages:
        plan_slide = bool(_PLAN_TITLE.search(page.title or ""))
        _extract_money(page, profile, plan_slide=plan_slide)
        _extract_percentages(page, profile, plan_slide=plan_slide)
        _extract_patterns(page, profile, plan_slide=plan_slide)
        _extract_by_title(page, profile)
    _infer_categoricals(doc, profile)
    return profile

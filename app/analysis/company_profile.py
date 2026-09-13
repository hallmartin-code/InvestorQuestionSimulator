"""Company profile construction: rule-based extraction, grounded model extraction, founder
input, review corrections and calculated fields.

Model-extracted values are accepted only when the cited page's text contains the quoted
evidence and the value's figures appear in that quote. "Inferred" values are allowed only for
categorical fields (sector, subsector, stage, business model, target customer) and are
labeled INFERRED. Nothing is silently inferred into a factual field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.analysis.rule_extractor import KEYWORD_CONFIDENCE, extract_rule_based
from app.ingestion.models import DeckDocument
from app.integrations.llm_client import StructuredLLM
from app.models.company import FIELD_SPECS, CompanyProfile, DataStatus, FieldValue, parse_field_value
from app.utils.money import money_mentions, parse_money
from app.utils.text import numbers_in, overlap_ratio, truncate

GROUNDING_THRESHOLD = 0.6
LLM_CONFIDENCE = 0.85
VISION_CONFIDENCE = 0.45

#: Optional company details offered on the upload screen.
UPLOAD_FIELDS: tuple[str, ...] = (
    "company_name", "sector", "stage", "geography", "raise_amount", "amount_committed", "valuation",
    "instrument", "revenue", "runway_months",
)

FIELD_MEANINGS: dict[str, str] = {
    "company_name": "company name",
    "company_description": "one-line description of what the company does",
    "sector": "industry sector (e.g. Healthcare, Fintech)",
    "subsector": "subsector within the sector",
    "geography": "headquarters location or primary geography",
    "stage": "company/financing stage (Pre-seed, Seed, Series A...)",
    "founding_year": "year founded",
    "problem": "the customer problem, in brief",
    "solution": "the proposed solution, in brief",
    "product": "what the product is and does today",
    "target_customer": "who buys",
    "target_market": "the market or segment targeted",
    "business_model": "how the company makes money",
    "pricing": "pricing as stated",
    "revenue": "achieved annual revenue (state the period in the quote)",
    "arr": "achieved annual recurring revenue",
    "mrr": "achieved monthly recurring revenue",
    "revenue_growth": "achieved revenue growth rate as a percentage",
    "customers": "customer description as stated (paying customers, logos)",
    "customer_count": "number of customers",
    "pilots": "pilots underway or completed",
    "contracts": "signed contracts, LOIs or purchase orders",
    "retention": "retention rate as a percentage",
    "churn": "churn rate as a percentage",
    "gross_margin": "gross margin as a percentage",
    "cac": "customer acquisition cost",
    "ltv": "customer lifetime value",
    "sales_cycle": "sales cycle length",
    "market_size": "market-size claim in the deck's words",
    "tam": "total addressable market amount",
    "sam": "serviceable addressable market amount",
    "som": "serviceable obtainable market amount",
    "market_source": "source cited for market size, only if the deck cites one",
    "competitors": "named competitors, separated by semicolons",
    "differentiation": "claimed differentiation",
    "intellectual_property": "patents, filings or other IP",
    "regulatory_status": "regulatory clearances, submissions or plans",
    "team": "founders and key team members with relevant background, in brief",
    "historical_financials": "past financial results, in brief",
    "financial_projections": "projected revenue or financial targets, in brief (these are assumptions)",
    "monthly_burn": "monthly cash burn",
    "prior_funding": "total capital raised before this round",
    "raise_amount": "amount being raised in the current round",
    "amount_committed": "amount already committed to the current round",
    "valuation": "valuation or valuation cap for the current round",
    "instrument": "financing instrument (SAFE, convertible note, priced equity)",
    "runway_months": "months of runway (number)",
    "use_of_funds": "planned uses of this round, separated by semicolons",
    "milestones": "milestones this round funds, separated by semicolons",
    "exit_strategy": "exit strategy or potential acquirers as stated",
}


class ExtractedField(BaseModel):
    field: str = Field(description="One of the field names listed in the instructions.")
    value: str = Field(description="The value as written in the material, e.g. \"$2.4M\" or \"68%\".")
    page: int = Field(description="Slide/page number the value appears on; 0 for founder notes.")
    quote: str = Field(description="Verbatim quote (under 40 words) from that page supporting the value.")
    basis: Literal["stated", "inferred"] = Field(description="stated = written in the material; inferred = clearly implied.")


class ExtractionOutput(BaseModel):
    fields: list[ExtractedField]


EXTRACTION_INSTRUCTIONS = """Task: extract company facts from the pitch deck (in <deck>) and any founder notes (in <founder_notes>) to build the company profile for a fundraising interview.

Fields you may return (name: meaning):
{field_list}

Rules:
- Return only values the material supports. For each value give the slide/page number (0 for founder notes) and a verbatim quote under 40 words copied from that page. Quotes are checked automatically against the page text, and paraphrased quotes are discarded.
- Use basis "stated" for values written in the material. Use basis "inferred" only for sector, subsector, stage, business_model and target_customer, and only when the material clearly implies them; quote the words that imply them.
- Write money as stated (e.g. "$2.4M"), percentages as stated (e.g. "68%"), runway_months as a number of months, founding_year as a four-digit year.
- revenue, arr, mrr, revenue_growth, customers and customer_count are results achieved to date. Projections, targets and plans belong in financial_projections, never in those fields.
- For list fields (competitors, use_of_funds, milestones) separate items with semicolons.
- Leave out any field the material does not support. A missing value is useful; an estimate is a defect."""


def extraction_instructions() -> str:
    field_list = "\n".join(f"- {name}: {meaning}" for name, meaning in FIELD_MEANINGS.items())
    return EXTRACTION_INSTRUCTIONS.format(field_list=field_list)


def build_context(doc: DeckDocument | None, notes: str) -> tuple[str, list[str]]:
    """The stable per-session prompt context: deck text with slide markers, plus founder notes."""
    warnings: list[str] = []
    parts: list[str] = []
    if doc is not None:
        text, warnings = doc.llm_context()
        parts.append(f"<deck filename=\"{doc.filename}\" slides=\"{doc.page_count}\">\n{text}\n</deck>")
    if notes.strip():
        parts.append(f"<founder_notes>\n{truncate(notes, 8000)}\n</founder_notes>")
    return "\n\n".join(parts), warnings


def _value_supported(kind: str, raw: str, quote: str) -> bool:
    if kind == "money":
        amount = parse_money(raw)
        if amount is None:
            return False
        if any(abs(value - amount) <= 0.01 * max(amount, 1) for _t, value, _o in money_mentions(quote)):
            return True
        return bool(numbers_in(raw) & numbers_in(quote))
    if kind in ("percent", "number", "months", "year"):
        return bool(numbers_in(raw) & numbers_in(quote))
    return True


def ground_extraction(doc: DeckDocument | None, notes: str, output: ExtractionOutput, *,
                      vision_pages: set[int] | None = None) -> tuple[list[FieldValue], int]:
    """Convert model output to FieldValues, discarding anything not traceable to its cited page."""
    accepted: list[FieldValue] = []
    rejected = 0
    for item in output.fields:
        spec = FIELD_SPECS.get(item.field)
        raw, quote = item.value.strip(), item.quote.strip()
        if spec is None or spec.calculated or not raw:
            rejected += 1
            continue
        value = parse_field_value(spec, raw)
        if value is None:
            rejected += 1
            continue
        if item.basis == "inferred" and not spec.inferable:
            rejected += 1
            continue

        if item.page == 0:
            source_text, page = notes, None
        elif doc is not None and 1 <= item.page <= doc.page_count:
            source_text, page = doc.page_text(item.page), item.page
        else:
            rejected += 1
            continue

        note = None
        if source_text.strip():
            if overlap_ratio(quote, source_text) < GROUNDING_THRESHOLD:
                rejected += 1
                continue
            if item.basis == "stated" and not _value_supported(spec.kind, raw, quote):
                rejected += 1
                continue
            confidence = LLM_CONFIDENCE
        elif page is not None and vision_pages and page in vision_pages:
            confidence = VISION_CONFIDENCE
            note = "Read from the slide image; cannot be verified against extracted text. Please confirm."
        else:
            rejected += 1
            continue

        if item.basis == "inferred":
            status, source, confidence = DataStatus.INFERRED, "model_inference", min(confidence, 0.5)
            note = note or "Inferred by the AI model from the quoted text; confirm."
        elif page is None:
            status, source = DataStatus.USER_PROVIDED, "user"
        else:
            status, source = DataStatus.VERIFIED_FROM_DECK, "deck"
        accepted.append(FieldValue(field=spec.name, value=value, status=status, source=source, page_or_slide=page,
                                   evidence=truncate(quote, 240), confidence=confidence, note=note))
    return accepted, rejected


def apply_user_inputs(profile: CompanyProfile, inputs: dict[str, str]) -> list[str]:
    """Record founder-entered values as USER_PROVIDED; return messages for unreadable entries."""
    warnings: list[str] = []
    for name, raw in inputs.items():
        spec = FIELD_SPECS.get(name)
        if spec is None or spec.calculated or raw is None or not str(raw).strip():
            continue
        value = parse_field_value(spec, str(raw))
        if value is None:
            warnings.append(f"Could not read “{truncate(str(raw), 40)}” as a value for {spec.label}; it was ignored.")
            continue
        profile.set(FieldValue(field=name, value=value, status=DataStatus.USER_PROVIDED, source="user",
                               evidence="Entered by the founder", confidence=1.0))
    return warnings


def apply_corrections(profile: CompanyProfile, edits: dict[str, str]) -> list[str]:
    """Apply review-screen edits. Unchanged values keep their provenance; changed ones become USER_PROVIDED."""
    warnings: list[str] = []
    for name, spec in FIELD_SPECS.items():
        if spec.calculated or name not in edits:
            continue
        submitted = " ".join(str(edits[name] or "").split())
        current = profile.get(name)
        current_display = " ".join((current.display if current else "").split())
        if submitted == current_display:
            continue
        if not submitted:
            if current is not None:
                profile.clear(name)
            continue
        value = parse_field_value(spec, submitted)
        if value is None:
            warnings.append(f"Could not read “{truncate(submitted, 40)}” for {spec.label}; the previous value was kept.")
            continue
        profile.set(FieldValue(field=name, value=value, status=DataStatus.USER_PROVIDED, source="user",
                               evidence="Corrected by the founder during review", confidence=1.0))
    derive_calculated(profile)
    return warnings


def derive_calculated(profile: CompanyProfile) -> None:
    """Recompute CALCULATED fields from current inputs (never overriding stated values)."""
    for name in [n for n, fv in profile.fields.items() if fv.status is DataStatus.CALCULATED]:
        del profile.fields[name]

    def put(field: str, value: float, formula: str, note: str | None = None) -> None:
        profile.set(FieldValue(field=field, value=value, status=DataStatus.CALCULATED, source="calculation",
                               evidence=formula, confidence=0.9, note=note))

    raise_amount, committed = profile.value("raise_amount"), profile.value("amount_committed")
    if raise_amount is not None and committed is not None:
        put("amount_remaining", max(float(raise_amount) - float(committed), 0.0), "Current raise − amount committed")
    cac, ltv = profile.value("cac"), profile.value("ltv")
    if cac and ltv is not None and float(cac) > 0:
        put("ltv_cac_ratio", round(float(ltv) / float(cac), 2), "LTV ÷ CAC")
    if profile.get("arr") is None and profile.value("mrr") is not None:
        put("arr", float(profile.value("mrr")) * 12, "MRR × 12")
    burn = profile.value("monthly_burn")
    if profile.get("runway_months") is None and raise_amount is not None and burn and float(burn) > 0:
        put("runway_months", round(float(raise_amount) / float(burn), 1), "Current raise ÷ monthly burn",
            note="Runway this raise buys at current burn; excludes existing cash.")


def build_profile(doc: DeckDocument | None, *, notes: str, user_inputs: dict[str, str], context: str,
                  llm: StructuredLLM | None, effort: str | None = None,
                  page_images: list[tuple[int, bytes]] | None = None) -> tuple[CompanyProfile, list[str], bool]:
    """Rule-based extraction, then grounded model extraction, then founder input and calculations.

    Returns the profile, user-facing warnings, and whether the model contributed.
    """
    warnings: list[str] = []
    profile = extract_rule_based(doc) if doc is not None else CompanyProfile()
    llm_used = False

    if llm is not None and (context or page_images):
        images = [(f"Slide {n} (image):", png) for n, png in (page_images or [])]
        output, error = llm.structured(label="extract_profile", output_model=ExtractionOutput,
                                       instructions=extraction_instructions(), context=context, images=images,
                                       effort=effort)
        if output is None:
            warnings.append(f"AI extraction was unavailable ({error}); rule-based extraction was used. "
                            "Check the extracted values carefully.")
        else:
            llm_used = True
            accepted, rejected = ground_extraction(doc, notes, output,
                                                   vision_pages={n for n, _ in page_images or []})
            confirmed = {fv.field for fv in accepted}
            # Low-confidence keyword guesses the model did not confirm are dropped as noise.
            for name, fv in list(profile.fields.items()):
                if fv.confidence <= KEYWORD_CONFIDENCE and fv.status is DataStatus.VERIFIED_FROM_DECK \
                        and name not in confirmed:
                    del profile.fields[name]
            for fv in accepted:
                profile.set(fv)
            if rejected:
                warnings.append(f"{rejected} AI-extracted value(s) could not be traced to the cited slide text and "
                                "were discarded.")

    warnings.extend(apply_user_inputs(profile, user_inputs))
    derive_calculated(profile)
    return profile, warnings, llm_used

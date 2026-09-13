"""Company profile with per-field provenance.

Every value records where it came from (deck page, user entry, calculation or inference),
the evidence it rests on and a confidence. Two classifications are kept deliberately apart:

* ``DataStatus`` - how the value entered the profile
  (VERIFIED_FROM_DECK, USER_PROVIDED, CALCULATED, INFERRED, MISSING).
* ``ClaimType`` - what kind of statement it is for an investor
  (FACT, FOUNDER_CLAIM, ASSUMPTION, CALCULATION, INFERENCE, MISSING). A figure printed in
  a deck is a founder claim, not a fact, unless it carries a third-party citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.utils.money import format_money, format_number, format_percent, parse_money, parse_number, parse_percent
from app.utils.text import truncate


class DataStatus(str, Enum):
    VERIFIED_FROM_DECK = "VERIFIED_FROM_DECK"
    USER_PROVIDED = "USER_PROVIDED"
    CALCULATED = "CALCULATED"
    INFERRED = "INFERRED"
    MISSING = "MISSING"


class ClaimType(str, Enum):
    FACT = "FACT"
    FOUNDER_CLAIM = "FOUNDER_CLAIM"
    ASSUMPTION = "ASSUMPTION"
    CALCULATION = "CALCULATION"
    INFERENCE = "INFERENCE"
    MISSING = "MISSING"


FieldKind = Literal["text", "money", "percent", "number", "months", "year", "list"]
SourceKind = Literal["deck", "user", "calculation", "model_inference", "rule_inference", "none"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    section: str
    kind: FieldKind
    inferable: bool = False
    calculated: bool = False


SECTION_LABELS: dict[str, str] = {
    "identity": "Company",
    "product": "Problem, product & customer",
    "business_model": "Business model",
    "traction": "Traction",
    "unit_economics": "Unit economics",
    "market": "Market",
    "competition": "Competition & defensibility",
    "team": "Team",
    "financials": "Financials",
    "fundraising": "Fundraising",
}

_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("company_name", "Company name", "identity", "text"),
    FieldSpec("company_description", "Description", "identity", "text"),
    FieldSpec("sector", "Sector", "identity", "text", inferable=True),
    FieldSpec("subsector", "Subsector", "identity", "text", inferable=True),
    FieldSpec("geography", "Headquarters / geography", "identity", "text"),
    FieldSpec("stage", "Company stage", "identity", "text", inferable=True),
    FieldSpec("founding_year", "Founding year", "identity", "year"),
    FieldSpec("problem", "Problem", "product", "text"),
    FieldSpec("solution", "Solution", "product", "text"),
    FieldSpec("product", "Product", "product", "text"),
    FieldSpec("target_customer", "Target customer", "product", "text", inferable=True),
    FieldSpec("target_market", "Target market", "product", "text"),
    FieldSpec("business_model", "Business model", "business_model", "text", inferable=True),
    FieldSpec("pricing", "Pricing", "business_model", "text"),
    FieldSpec("revenue", "Annual revenue", "traction", "money"),
    FieldSpec("arr", "ARR", "traction", "money"),
    FieldSpec("mrr", "MRR", "traction", "money"),
    FieldSpec("revenue_growth", "Revenue growth", "traction", "percent"),
    FieldSpec("customers", "Customers", "traction", "text"),
    FieldSpec("customer_count", "Customer count", "traction", "number"),
    FieldSpec("pilots", "Pilots", "traction", "text"),
    FieldSpec("contracts", "Contracts / LOIs", "traction", "text"),
    FieldSpec("retention", "Retention", "traction", "percent"),
    FieldSpec("churn", "Churn", "traction", "percent"),
    FieldSpec("gross_margin", "Gross margin", "unit_economics", "percent"),
    FieldSpec("cac", "CAC", "unit_economics", "money"),
    FieldSpec("ltv", "LTV", "unit_economics", "money"),
    FieldSpec("ltv_cac_ratio", "LTV:CAC", "unit_economics", "number", calculated=True),
    FieldSpec("sales_cycle", "Sales cycle", "unit_economics", "text"),
    FieldSpec("market_size", "Market size (as described)", "market", "text"),
    FieldSpec("tam", "TAM", "market", "money"),
    FieldSpec("sam", "SAM", "market", "money"),
    FieldSpec("som", "SOM", "market", "money"),
    FieldSpec("market_source", "Market-size source", "market", "text"),
    FieldSpec("competitors", "Competitors", "competition", "list"),
    FieldSpec("differentiation", "Differentiation", "competition", "text"),
    FieldSpec("intellectual_property", "Intellectual property", "competition", "text"),
    FieldSpec("regulatory_status", "Regulatory status", "competition", "text"),
    FieldSpec("team", "Team", "team", "text"),
    FieldSpec("historical_financials", "Historical financials", "financials", "text"),
    FieldSpec("financial_projections", "Financial projections", "financials", "text"),
    FieldSpec("monthly_burn", "Monthly burn", "financials", "money"),
    FieldSpec("prior_funding", "Prior funding (raised to date)", "fundraising", "money"),
    FieldSpec("raise_amount", "Current raise", "fundraising", "money"),
    FieldSpec("amount_committed", "Amount committed", "fundraising", "money"),
    FieldSpec("amount_remaining", "Amount remaining to raise", "fundraising", "money", calculated=True),
    FieldSpec("valuation", "Valuation / cap", "fundraising", "money"),
    FieldSpec("instrument", "Financing instrument", "fundraising", "text"),
    FieldSpec("runway_months", "Runway (months)", "fundraising", "months"),
    FieldSpec("use_of_funds", "Use of funds", "fundraising", "list"),
    FieldSpec("milestones", "Milestones", "fundraising", "list"),
    FieldSpec("exit_strategy", "Exit strategy", "fundraising", "text"),
)

FIELD_SPECS: dict[str, FieldSpec] = {spec.name: spec for spec in _SPECS}

#: Fields describing plans or forecasts: always assumptions, never facts.
PROJECTION_FIELDS = frozenset({"financial_projections", "milestones", "exit_strategy", "use_of_funds"})

_SOURCE_CITATION = re.compile(
    r"\b(source[sd]?:|according to|gartner|mckinsey|statista|grand view|frost|idc\b|forrester|cb insights|"
    r"pitchbook|census|bureau|cms\.gov|world bank|oecd|who\b|nih\b|cdc\b|bls\b|marketsandmarkets|"
    r"fortune business insights|precedence research|report\b)",
    re.IGNORECASE,
)


def classify_claim(field: str, status: DataStatus, evidence: str | None) -> ClaimType:
    if status is DataStatus.MISSING:
        return ClaimType.MISSING
    if status is DataStatus.CALCULATED:
        return ClaimType.CALCULATION
    if status is DataStatus.INFERRED:
        return ClaimType.INFERENCE
    if field in PROJECTION_FIELDS:
        return ClaimType.ASSUMPTION
    if status is DataStatus.VERIFIED_FROM_DECK and evidence and _SOURCE_CITATION.search(evidence):
        return ClaimType.FACT
    return ClaimType.FOUNDER_CLAIM


def parse_field_value(spec: FieldSpec, raw: str | None) -> Any | None:
    """Convert raw text to the field's value type; ``None`` when it cannot be read."""
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    if spec.kind == "money":
        return parse_money(text)
    if spec.kind == "percent":
        return parse_percent(text)
    if spec.kind in ("number", "months"):
        value = parse_number(text)
        return value if value is None or value >= 0 else None
    if spec.kind == "year":
        match = re.search(r"\b(19|20)\d{2}\b", text)
        return int(match.group(0)) if match else None
    if spec.kind == "list":
        separator = ";" if ";" in text or "\n" in str(raw) else ","
        parts = re.split(rf"{separator}|\n", str(raw))
        items = [truncate(p.strip(" •-–*\t"), 120) for p in parts]
        items = [i for i in items if i]
        return items[:12] or None
    return truncate(text, 600)


def format_field_value(spec: FieldSpec, value: Any) -> str:
    if value is None:
        return ""
    if spec.kind == "money":
        return format_money(float(value))
    if spec.kind == "percent":
        return format_percent(float(value))
    if spec.kind == "months":
        return f"{format_number(float(value))} months"
    if spec.kind == "year":
        return str(int(value))
    if spec.kind == "number":
        if spec.name == "ltv_cac_ratio":
            return f"{float(value):.1f}x"
        return format_number(float(value))
    if spec.kind == "list":
        return "; ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return str(value)


class FieldValue(BaseModel):
    field: str
    value: Any = None
    display: str = ""
    status: DataStatus
    claim_type: ClaimType | None = None
    source: SourceKind = "deck"
    page_or_slide: int | None = None
    evidence: str | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    note: str | None = None

    @model_validator(mode="after")
    def _derive(self) -> FieldValue:
        spec = FIELD_SPECS.get(self.field)
        if spec is not None and not self.display and self.value is not None:
            self.display = format_field_value(spec, self.value)
        if self.claim_type is None:
            self.claim_type = classify_claim(self.field, self.status, self.evidence)
        return self

    @property
    def label(self) -> str:
        spec = FIELD_SPECS.get(self.field)
        return spec.label if spec else self.field

    @property
    def location(self) -> str:
        if self.source == "user":
            return "Founder input"
        if self.source == "calculation":
            return "Calculated"
        if self.page_or_slide:
            return f"Slide {self.page_or_slide}"
        return "Deck" if self.source == "deck" else "Inferred"

    def provenance(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "display": self.display,
            "status": self.status.value,
            "claim_type": self.claim_type.value if self.claim_type else None,
            "source": self.source,
            "page_or_slide": self.page_or_slide,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
            "note": self.note,
        }


_STATUS_PRIORITY = {
    DataStatus.USER_PROVIDED: 4,
    DataStatus.VERIFIED_FROM_DECK: 3,
    DataStatus.CALCULATED: 2,
    DataStatus.INFERRED: 1,
    DataStatus.MISSING: 0,
}


def values_conflict(spec: FieldSpec, a: Any, b: Any) -> bool:
    if a is None or b is None or spec.kind not in ("money", "percent", "number", "months", "year"):
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    scale = max(abs(fa), abs(fb))
    return scale > 0 and abs(fa - fb) > 0.1 * scale


class CompanyProfile(BaseModel):
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    #: Conflicting values seen for a field (e.g. two different revenue figures on two slides).
    alternates: dict[str, list[FieldValue]] = Field(default_factory=dict)

    # --- access -----------------------------------------------------------------------

    def get(self, name: str) -> FieldValue | None:
        fv = self.fields.get(name)
        return None if fv is None or fv.status is DataStatus.MISSING else fv

    def value(self, name: str, default: Any = None) -> Any:
        fv = self.get(name)
        return default if fv is None or fv.value is None else fv.value

    def display(self, name: str, default: str = "") -> str:
        fv = self.get(name)
        return fv.display if fv else default

    def has(self, *names: str) -> bool:
        return any(self.get(n) is not None for n in names)

    @property
    def company_name(self) -> str:
        return self.display("company_name") or "The company"

    def missing_fields(self) -> list[str]:
        return [name for name in FIELD_SPECS if self.get(name) is None]

    # --- mutation ---------------------------------------------------------------------

    def set(self, new: FieldValue) -> bool:
        """Merge a value by source priority; conflicting deck figures are kept as alternates."""
        spec = FIELD_SPECS[new.field]
        existing = self.fields.get(new.field)
        if existing is None or existing.status is DataStatus.MISSING or new.status is DataStatus.USER_PROVIDED:
            self.fields[new.field] = new
            return True
        if existing.status is DataStatus.USER_PROVIDED:
            return False
        new_rank, old_rank = _STATUS_PRIORITY[new.status], _STATUS_PRIORITY[existing.status]
        replace = new_rank > old_rank or (new_rank == old_rank and new.confidence > existing.confidence)
        loser = existing if replace else new
        winner = new if replace else existing
        if (loser.status is DataStatus.VERIFIED_FROM_DECK and winner.status is DataStatus.VERIFIED_FROM_DECK
                and loser.page_or_slide != winner.page_or_slide
                and values_conflict(spec, loser.value, winner.value)):
            self.alternates.setdefault(new.field, []).append(loser)
        if replace:
            self.fields[new.field] = new
        return replace

    def clear(self, name: str, note: str = "Cleared by founder during review") -> None:
        self.fields[name] = FieldValue(field=name, status=DataStatus.MISSING, source="none", confidence=0.0, note=note)
        self.alternates.pop(name, None)

    # --- views ------------------------------------------------------------------------

    def to_structured(self) -> dict[str, Any]:
        """The nested company-profile schema (values only)."""
        top_level = {"identity", "product", "business_model"}
        out: dict[str, Any] = {}
        for spec in _SPECS:
            value = self.value(spec.name)
            if spec.section in top_level:
                out[spec.name] = value if value is not None else ""
            else:
                out.setdefault(spec.section, {})[spec.name] = value
        fundraising = out.setdefault("fundraising", {})
        fundraising.setdefault("use_of_funds", [])
        if fundraising.get("use_of_funds") is None:
            fundraising["use_of_funds"] = []
        return out

    def sections(self) -> list[tuple[str, str, list[FieldSpec]]]:
        grouped: dict[str, list[FieldSpec]] = {}
        for spec in _SPECS:
            grouped.setdefault(spec.section, []).append(spec)
        return [(key, SECTION_LABELS[key], specs) for key, specs in grouped.items()]


_STAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("pre_seed", r"pre[\s-]?seed|angel|friends and family"),
    ("series_b_plus", r"series\s+[b-h]\b|growth|late[\s-]?stage"),
    ("series_a", r"series\s+a\b"),
    ("seed", r"\bseed\b"),
)

STAGE_LABELS = {"pre_seed": "Pre-seed", "seed": "Seed", "series_a": "Series A",
                "series_b_plus": "Series B+", "unknown": "Stage not stated"}


def stage_key(text: str | None) -> str:
    """Normalize a free-text stage/round description."""
    lowered = (text or "").lower()
    for key, pattern in _STAGE_PATTERNS:
        if re.search(pattern, lowered):
            return key
    return "unknown"

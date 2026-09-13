"""Evidence mapping: which profile fields bear on which interview topic, and with what provenance."""

from __future__ import annotations

from typing import Any

from app.models.company import FIELD_SPECS, CompanyProfile, FieldValue
from app.models.interview import Category
from app.utils.text import truncate

CATEGORY_FIELDS: dict[Category, tuple[str, ...]] = {
    Category.MARKET: ("tam", "sam", "som", "market_size", "market_source", "target_market"),
    Category.PROBLEM: ("problem", "target_customer"),
    Category.PRODUCT_SOLUTION: ("product", "solution", "company_description"),
    Category.CUSTOMER: ("target_customer", "customers", "customer_count", "pilots"),
    Category.TRACTION: ("revenue", "arr", "mrr", "revenue_growth", "customers", "customer_count", "pilots",
                        "contracts", "retention", "churn"),
    Category.BUSINESS_MODEL: ("business_model", "pricing", "gross_margin"),
    Category.UNIT_ECONOMICS: ("cac", "ltv", "ltv_cac_ratio", "gross_margin", "sales_cycle", "churn"),
    Category.GO_TO_MARKET: ("target_customer", "sales_cycle", "pricing", "cac"),
    Category.COMPETITION: ("competitors", "differentiation"),
    Category.DEFENSIBILITY: ("intellectual_property", "differentiation", "regulatory_status"),
    Category.TEAM: ("team",),
    Category.FINANCIAL_PERFORMANCE: ("revenue", "historical_financials", "monthly_burn", "gross_margin"),
    Category.FINANCIAL_PROJECTIONS: ("financial_projections", "revenue", "arr"),
    Category.RISKS: ("regulatory_status", "runway_months", "monthly_burn"),
    Category.FUNDRAISING: ("raise_amount", "amount_committed", "amount_remaining", "instrument", "prior_funding",
                           "runway_months"),
    Category.VALUATION: ("valuation", "raise_amount", "revenue", "arr"),
    Category.USE_OF_FUNDS: ("use_of_funds", "raise_amount", "runway_months"),
    Category.MILESTONES: ("milestones", "runway_months"),
    Category.EXIT_POTENTIAL: ("exit_strategy", "competitors"),
}


def primary_category(field: str) -> Category:
    for category, fields in CATEGORY_FIELDS.items():
        if field in fields:
            return category
    return Category.PRODUCT_SOLUTION


def fields_for_category(profile: CompanyProfile, category: Category) -> tuple[list[FieldValue], list[str]]:
    present: list[FieldValue] = []
    missing: list[str] = []
    for name in CATEGORY_FIELDS.get(category, ()):
        fv = profile.get(name)
        if fv is None:
            missing.append(name)
        else:
            present.append(fv)
    return present, missing


def describe_field(fv: FieldValue, *, evidence_chars: int = 140) -> str:
    parts = [fv.status.value, fv.claim_type.value if fv.claim_type else "", fv.location]
    tag = " · ".join(p for p in parts if p)
    evidence = f' — "{truncate(fv.evidence, evidence_chars)}"' if fv.evidence else ""
    note = f" (note: {fv.note})" if fv.note else ""
    return f"{fv.label}: {truncate(fv.display, 300)} [{tag}]{evidence}{note}"


def profile_digest(profile: CompanyProfile) -> str:
    """The profile as prompt text: every known field with provenance, then the missing fields."""
    lines = [f"- {describe_field(fv)}" for name in FIELD_SPECS if (fv := profile.get(name)) is not None]
    missing = [FIELD_SPECS[n].label for n in profile.missing_fields() if not FIELD_SPECS[n].calculated]
    alternates = [
        f"- {FIELD_SPECS[name].label}: also stated as {', '.join(f'{a.display} ({a.location})' for a in values)}"
        for name, values in profile.alternates.items()
    ]
    out = ["Known fields:", *(lines or ["- none"])]
    if alternates:
        out += ["Conflicting figures in the deck:", *alternates]
    out += ["Missing fields: " + (", ".join(missing) if missing else "none")]
    return "\n".join(out)


def category_digest(profile: CompanyProfile, category: Category) -> str:
    present, missing = fields_for_category(profile, category)
    lines = [f"- {describe_field(fv)}" for fv in present] or ["- nothing in the deck or founder input"]
    if missing:
        lines.append("- Missing: " + ", ".join(FIELD_SPECS[m].label for m in missing))
    return "\n".join(lines)


def build_evidence_map(profile: CompanyProfile) -> list[dict[str, Any]]:
    """Provenance record for every field, including MISSING entries."""
    entries: list[dict[str, Any]] = []
    for name, spec in FIELD_SPECS.items():
        fv = profile.get(name)
        if fv is None:
            entries.append({"field": name, "label": spec.label, "value": None, "status": "MISSING",
                            "claim_type": "MISSING", "source": "none", "page_or_slide": None,
                            "evidence": None, "confidence": 0.0})
        else:
            entries.append({"label": spec.label, **fv.provenance()})
    return entries

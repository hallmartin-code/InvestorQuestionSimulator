from __future__ import annotations

import pytest

from app.analysis.company_profile import (
    ExtractionOutput,
    apply_corrections,
    build_context,
    build_profile,
    ground_extraction,
)
from app.analysis.evidence_mapper import build_evidence_map
from app.ingestion.deck_loader import load_deck
from app.models.company import ClaimType, CompanyProfile, DataStatus, FieldValue
from tests.conftest import FakeLLM


@pytest.fixture
def sample_doc(settings, fixture_files):
    path = fixture_files["sample_pdf"]
    return load_deck(path.name, path.read_bytes(), content_type=None, settings=settings)


def _profile(doc, **kwargs):
    context, _ = build_context(doc, kwargs.pop("notes", ""))
    return build_profile(doc, notes="", user_inputs=kwargs.pop("user_inputs", {}), context=context,
                         llm=kwargs.pop("llm", None))


def test_rule_based_extraction_preserves_provenance(sample_doc):
    profile, warnings, used = _profile(sample_doc)
    assert not used and warnings == []
    raise_amount = profile.get("raise_amount")
    assert raise_amount.value == 4_000_000
    assert raise_amount.status is DataStatus.VERIFIED_FROM_DECK
    assert raise_amount.page_or_slide == 10
    assert "Raising $4M" in raise_amount.evidence
    assert profile.value("tam") == 6_200_000_000 and profile.get("tam").page_or_slide == 4
    assert profile.value("arr") == 1_200_000
    assert profile.value("amount_committed") == 1_500_000
    assert profile.value("valuation") == 20_000_000
    assert profile.value("runway_months") == 18
    assert profile.display("instrument") == "SAFE"
    assert "$85K per hospital" in profile.display("pricing")


def test_projections_are_never_recorded_as_achieved_revenue(sample_doc):
    profile, _, _ = _profile(sample_doc)
    assert profile.get("revenue") is None
    projections = profile.get("financial_projections")
    assert projections is not None and "2028" in projections.display
    assert projections.claim_type is ClaimType.ASSUMPTION
    assert "customer_count" not in profile.alternates  # "Reach 40 hospitals" is a milestone, not a count
    assert profile.value("customer_count") == 14


def test_deck_figures_are_founder_claims_and_inferences_are_labeled(sample_doc):
    profile, _, _ = _profile(sample_doc)
    assert profile.get("arr").claim_type is ClaimType.FOUNDER_CLAIM
    sector = profile.get("sector")
    assert sector.status is DataStatus.INFERRED and sector.claim_type is ClaimType.INFERENCE
    sourced = FieldValue(field="tam", value=1e9, status=DataStatus.VERIFIED_FROM_DECK,
                         evidence="TAM $1B (Source: Grand View Research, 2025)")
    assert sourced.claim_type is ClaimType.FACT


def test_missing_data_is_reported_as_missing(settings, fixture_files):
    path = fixture_files["sparse_pdf"]
    doc = load_deck(path.name, path.read_bytes(), content_type=None, settings=settings)
    profile, _, _ = _profile(doc)
    missing = profile.missing_fields()
    for name in ("raise_amount", "revenue", "tam", "team", "use_of_funds"):
        assert name in missing
    entries = {e["field"]: e for e in build_evidence_map(profile)}
    assert entries["raise_amount"]["status"] == "MISSING" and entries["raise_amount"]["value"] is None


def test_user_inputs_take_precedence_and_bad_values_are_reported(sample_doc):
    profile, warnings, _ = _profile(sample_doc, user_inputs={"raise_amount": "$5M", "runway_months": "soon"})
    fv = profile.get("raise_amount")
    assert fv.value == 5_000_000 and fv.status is DataStatus.USER_PROVIDED and fv.claim_type is ClaimType.FOUNDER_CLAIM
    assert any("Runway" in w for w in warnings)
    assert profile.value("runway_months") == 18  # the deck value is kept


def test_calculated_fields(sample_doc):
    profile, _, _ = _profile(sample_doc)
    remaining = profile.get("amount_remaining")
    assert remaining.value == 2_500_000 and remaining.status is DataStatus.CALCULATED
    assert remaining.claim_type is ClaimType.CALCULATION


def test_review_corrections(sample_doc):
    profile, _, _ = _profile(sample_doc)
    tam_status = profile.get("tam").status
    warnings = apply_corrections(profile, {"raise_amount": "$6M", "tam": profile.display("tam"), "pilots": "",
                                           "valuation": "a lot"})
    assert profile.get("raise_amount").status is DataStatus.USER_PROVIDED
    assert profile.value("amount_remaining") == 4_500_000  # recalculated from the corrected raise
    assert profile.get("tam").status is tam_status  # unchanged value keeps its provenance
    assert profile.get("pilots") is None
    assert any("Valuation" in w for w in warnings) and profile.value("valuation") == 20_000_000


def _extraction(*fields):
    return ExtractionOutput.model_validate({"fields": list(fields)})


def test_grounding_rejects_fabricated_quotes_and_unsupported_values(sample_doc):
    output = _extraction(
        {"field": "arr", "value": "$1.2M", "page": 5, "quote": "$1.2M ARR as of June 2025", "basis": "stated"},
        {"field": "revenue", "value": "$9M", "page": 5, "quote": "We generated $9M in revenue last year", "basis": "stated"},
        {"field": "tam", "value": "$9B", "page": 4, "quote": "TAM $6.2B", "basis": "stated"},
        {"field": "raise_amount", "value": "$4M", "page": 99, "quote": "Raising $4M", "basis": "stated"},
        {"field": "valuation", "value": "$20M", "page": 10, "quote": "$20M valuation cap", "basis": "inferred"},
        {"field": "sector", "value": "Healthcare", "page": 1, "quote": "AI discharge coordination for community hospitals", "basis": "inferred"},
    )
    accepted, rejected = ground_extraction(sample_doc, "", output)
    by_field = {fv.field: fv for fv in accepted}
    assert set(by_field) == {"arr", "sector"}
    assert rejected == 4
    assert by_field["arr"].confidence == 0.85 and by_field["arr"].status is DataStatus.VERIFIED_FROM_DECK
    assert by_field["sector"].status is DataStatus.INFERRED


def test_founder_notes_extraction_is_user_provided(sample_doc):
    notes = "Since the deck we signed 2 more hospitals; ARR is now $1.5M."
    output = _extraction({"field": "arr", "value": "$1.5M", "page": 0, "quote": "ARR is now $1.5M", "basis": "stated"})
    accepted, _ = ground_extraction(sample_doc, notes, output)
    assert accepted[0].status is DataStatus.USER_PROVIDED and accepted[0].source == "user"


def test_model_extraction_merges_and_failure_falls_back(sample_doc):
    llm = FakeLLM({"extract_profile": {"fields": [
        {"field": "target_customer", "value": "Community hospitals", "page": 1,
         "quote": "AI discharge coordination for community hospitals", "basis": "inferred"},
        {"field": "competitors", "value": "Epic; Cerner", "page": 7, "quote": "Epic Cerner", "basis": "stated"},
    ]}})
    profile, warnings, used = _profile(sample_doc, llm=llm)
    assert used and profile.get("target_customer").status is DataStatus.INFERRED
    assert profile.value("competitors") == ["Epic", "Cerner"]
    assert "<deck" in llm.calls[0]["context"]

    failing = FakeLLM(fail={"extract_profile"})
    profile, warnings, used = _profile(sample_doc, llm=failing)
    assert not used and any("rule-based extraction was used" in w for w in warnings)
    assert profile.value("raise_amount") == 4_000_000


def test_structured_profile_schema(sample_doc):
    profile, _, _ = _profile(sample_doc)
    structured = profile.to_structured()
    assert structured["company_name"] == "CareLoop Health"
    assert structured["fundraising"]["raise_amount"] == 4_000_000
    assert set(["raise_amount", "amount_committed", "valuation", "instrument", "runway_months", "use_of_funds"]) <= set(
        structured["fundraising"])
    assert structured["traction"]["arr"] == 1_200_000
    empty = CompanyProfile().to_structured()
    assert empty["fundraising"]["use_of_funds"] == [] and empty["fundraising"]["raise_amount"] is None

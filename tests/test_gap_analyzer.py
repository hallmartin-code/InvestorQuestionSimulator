from __future__ import annotations

from app.analysis.gap_analyzer import analyze_gaps, rule_based_issues
from app.ingestion.models import DeckDocument, DeckPage
from app.models.analysis import IssueTag
from app.models.company import CompanyProfile, DataStatus, FieldValue
from app.models.interview import Category
from tests.conftest import FakeLLM


def make_profile(**values) -> CompanyProfile:
    profile = CompanyProfile()
    for page, (name, value) in enumerate(values.items(), start=1):
        profile.set(FieldValue(field=name, value=value, status=DataStatus.VERIFIED_FROM_DECK, page_or_slide=page,
                               evidence=f"{name} {value}", confidence=0.6))
    return profile


def tags(issues, category=None):
    return [(i.tag, i.category) for i in issues if category is None or i.category == category]


def test_missing_information_becomes_evidence_gaps():
    issues = rule_based_issues(None, make_profile(stage="Seed", problem="x", team="y"))
    fundraising = [i for i in issues if i.category is Category.FUNDRAISING and i.tag is IssueTag.EVIDENCE_GAP]
    assert fundraising and fundraising[0].severity == 3
    assert "raise_amount" in fundraising[0].fields


def test_series_a_expects_unit_economics():
    issues = rule_based_issues(None, make_profile(stage="Series A"))
    gap = next(i for i in issues if i.category is Category.UNIT_ECONOMICS and i.tag is IssueTag.EVIDENCE_GAP)
    assert {"gross_margin", "cac", "ltv"} <= set(gap.fields)


def test_market_sizing_inconsistency_and_unsourced_tam():
    issues = rule_based_issues(None, make_profile(tam=1e9, sam=2e9, som=5e7))
    assert (IssueTag.INCONSISTENCY, Category.MARKET) in tags(issues)
    assert any(i.title == "Market size is unsourced" for i in issues)
    sourced = rule_based_issues(None, make_profile(tam=1e9, market_source="Grand View Research"))
    assert not any(i.title == "Market size is unsourced" for i in sourced)


def test_fundraising_arithmetic():
    issues = rule_based_issues(None, make_profile(raise_amount=2e6, amount_committed=3e6, runway_months=4))
    assert any(i.tag is IssueTag.INCONSISTENCY and i.severity == 3 and "exceed" in i.title for i in issues)
    assert any(i.tag is IssueTag.RED_FLAG and "Runway" in i.title for i in issues)


def test_conflicting_figures_across_slides():
    profile = CompanyProfile()
    profile.set(FieldValue(field="arr", value=1.2e6, status=DataStatus.VERIFIED_FROM_DECK, page_or_slide=3, confidence=0.6))
    profile.set(FieldValue(field="arr", value=2.0e6, status=DataStatus.VERIFIED_FROM_DECK, page_or_slide=9, confidence=0.85))
    issues = rule_based_issues(None, profile)
    conflict = next(i for i in issues if i.tag is IssueTag.INCONSISTENCY and "ARR" in i.title)
    assert "Slide 3" in conflict.detail and "Slide 9" in conflict.detail


def test_unit_economics_and_language_red_flags():
    profile = make_profile(cac=50_000, ltv=30_000)
    from app.analysis.company_profile import derive_calculated

    derive_calculated(profile)
    doc = DeckDocument(filename="d.pdf", source_format="pdf", pages=[
        DeckPage(number=1, title="Competition", text="Competition\nThere are no competitors in this space."),
        DeckPage(number=2, title="Vision", text="Vision\nA revolutionary platform"),
    ])
    issues = rule_based_issues(doc, profile)
    assert any(i.tag is IssueTag.RED_FLAG and i.category is Category.UNIT_ECONOMICS for i in issues)
    red = next(i for i in issues if i.tag is IssueTag.RED_FLAG and i.category is Category.COMPETITION)
    assert red.evidence and "Slide 1" in red.evidence[0]
    assert any(i.tag is IssueTag.QUESTION and "revolutionary" in i.title for i in issues)


def test_strengths_are_recorded():
    issues = rule_based_issues(None, make_profile(arr=1.2e6, retention=115, intellectual_property="Two granted US patents"))
    strengths = [i.title for i in issues if i.tag is IssueTag.STRENGTH]
    assert "Revenue traction stated" in strengths and "High retention stated" in strengths
    assert "Patent protection stated" in strengths


def test_model_findings_are_grounded_and_deduplicated():
    doc = DeckDocument(filename="d.pdf", source_format="pdf", pages=[
        DeckPage(number=1, title="Traction", text="Traction\nWe grew 40% last quarter with 12 customers"),
    ])
    llm = FakeLLM({"gap_analysis": {"issues": [
        {"tag": "RED_FLAG", "category": "traction", "severity": 3, "title": "Customers are all pilots",
         "detail": "d", "evidence": [{"page": 1, "quote": "all customers are unpaid pilots"}]},
        {"tag": "QUESTION", "category": "traction", "severity": 2, "title": "Growth period unclear",
         "detail": "d", "evidence": [{"page": 1, "quote": "We grew 40% last quarter"}]},
        {"tag": "EVIDENCE_GAP", "category": "fundraising", "severity": 3, "title": "Missing: Current raise",
         "detail": "duplicate of a rule finding", "evidence": []},
    ]}})
    result = analyze_gaps(doc, make_profile(stage="Seed"), context="<deck/>", llm=llm)
    llm_issues = [i for i in result.issues if i.source == "llm"]
    assert result.llm_used
    downgraded = next(i for i in llm_issues if i.title == "Customers are all pilots")
    assert downgraded.tag is IssueTag.QUESTION and downgraded.evidence == []
    grounded = next(i for i in llm_issues if i.title == "Growth period unclear")
    assert grounded.evidence == ['Slide 1: "We grew 40% last quarter"']
    assert not any(i.title.startswith("Missing: Current raise") for i in llm_issues)


def test_model_failure_keeps_rule_findings():
    result = analyze_gaps(None, make_profile(stage="Seed"), context="", llm=FakeLLM(fail={"gap_analysis"}))
    assert not result.llm_used and result.issues and "unavailable" in result.warnings[0]

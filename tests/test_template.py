from __future__ import annotations

import json
import re

import pymupdf
import pytest

from app.errors import ConfigError
from app.models.scoring import READINESS_CATEGORIES
from app.reports.pdf_generator import generate_pdf
from app.reports.report_data import build_report_data
from app.reports.template import (
    MARKDOWN_NAME,
    REQUIRED_SECTIONS,
    TEMPLATE_DIR,
    TEMPLATE_PATH,
    load_template,
    render_markdown,
    render_template_pdf,
    write_template_artifacts,
)

#: Terms from the synthetic test company; none may appear in the general template.
COMPANY_TERMS = ("careloop", "hospital", "discharge", "readmission", "dana", "okafor", "columbus", "epic", "cerner",
                 "ehr", "payer")
MONEY_FIGURE = re.compile(r"[$€£]\s?\d")


def pdf_text(data: bytes) -> tuple[int, str]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc.page_count, "\n".join(page.get_text() for page in doc)


def write_variant(tmp_path, mutate) -> object:
    raw = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "variant.template.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_template_loads_with_sections_in_page_order():
    template = load_template()
    assert [s.id for s in template.ordered_sections()] == list(REQUIRED_SECTIONS)
    assert template.document.max_pages == 1 and len(template.fit_ladder) >= 1


def test_template_holds_structure_only():
    for text in (TEMPLATE_PATH.read_text(encoding="utf-8"), render_markdown()):
        lowered = text.lower()
        assert not [term for term in COMPANY_TERMS if term in lowered]
        assert MONEY_FIGURE.search(text) is None


def test_category_rows_follow_the_scoring_configuration():
    rows = load_template().section("category_scores").category_rows
    assert [(r.key, r.label, r.weight, tuple(r.topics)) for r in rows] == \
        [(rc.key, rc.label, rc.weight, rc.categories) for rc in READINESS_CATEGORIES]
    assert sum(r.weight for r in rows) == 100


def test_generated_report_follows_the_template(completed_session, service):
    template = load_template()
    _pages, text = pdf_text(service.report_pdf(completed_session))
    for section in template.sections:
        if section.heading:
            assert section.heading.upper() in text
    for row in template.section("fundraising_snapshot").snapshot_rows:
        assert row.label in text or any(label in text for label in row.alternate_labels.values())
    assert template.document.title in text and template.document.confidentiality_notice in text


def test_template_edits_flow_into_the_report(tmp_path, completed_session):
    def mutate(raw):
        concerns = next(s for s in raw["sections"] if s["id"] == "investor_concerns")
        concerns["heading"] = "Key investor objections"
        concerns["max_items"] = 2
        snapshot = next(s for s in raw["sections"] if s["id"] == "fundraising_snapshot")
        snapshot["snapshot_rows"][0]["label"] = "Round size"

    template = load_template(write_variant(tmp_path, mutate))
    data = build_report_data(completed_session, template=template)
    assert len(data.concerns) <= 2 and data.snapshot[0][0] == "Round size"
    _pages, text = pdf_text(generate_pdf(data, template=template))
    assert "KEY INVESTOR OBJECTIONS" in text and "Round size" in text


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda raw: raw["sections"].pop(), "missing sections: footer"),
    (lambda raw: next(s for s in raw["sections"] if s["id"] == "category_scores")["category_rows"][0].update(weight=20),
     "READINESS_CATEGORIES"),
    (lambda raw: next(s for s in raw["sections"] if s["id"] == "fundraising_snapshot")["snapshot_rows"][0].update(
        source="profile.not_a_field"), "unknown profile field"),
    (lambda raw: raw["document"].update(titel="typo"), "titel"),
    (lambda raw: next(s for s in raw["sections"] if s["id"] == "header")["fields"].pop(), "missing fields: raise"),
])
def test_invalid_templates_are_rejected(tmp_path, mutate, message):
    with pytest.raises(ConfigError, match=message):
        load_template(write_variant(tmp_path, mutate))


def test_unreadable_template_is_rejected(tmp_path):
    path = tmp_path / "broken.template.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not be read"):
        load_template(path)


def test_blank_template_pdf_shows_placeholders_only():
    pages, text = pdf_text(render_template_pdf())
    assert pages == 1
    for placeholder in ("[Company Name]", "[XX]", "[READINESS CLASSIFICATION]", "[Raise Amount]", "[NN]",
                        "[Practice question targeting a weak category]", "Confidential – for recipients only"):
        assert placeholder in text
    lowered = text.lower()
    assert not [term for term in COMPANY_TERMS if term in lowered]
    assert MONEY_FIGURE.search(text) is None


def test_committed_artifacts_are_current(tmp_path):
    markdown, pdf = write_template_artifacts(tmp_path)
    committed = TEMPLATE_DIR / MARKDOWN_NAME
    assert committed.read_text(encoding="utf-8") == markdown.read_text(encoding="utf-8"), \
        "REPORT_TEMPLATE.md is stale: run python -m app.reports.template"
    assert pdf.read_bytes().startswith(b"%PDF") and (TEMPLATE_DIR / pdf.name).is_file()


def test_download_filename_uses_the_template_pattern():
    assert load_template().filename("Acme_Robotics") == "Fundraising_Interview_Readiness_Acme_Robotics.pdf"

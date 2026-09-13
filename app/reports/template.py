"""The Fundraising Interview Readiness Report template: the single source of truth for the report's structure.

``templates/readiness_report.template.json`` defines the page geometry, type scale, fit ladder, section
order and headings, every field (label, type, source binding, format, length limit, placeholder, empty
text), the fundraising-snapshot rows, the category-score rows, the footer, the controlled vocabularies
and the analysis rules. It holds no company-specific content.

The PDF generator and report-data builder read their structure from here, and ``render_template_pdf``
renders the template itself through the ordinary renderer, so the template cannot describe a layout the
application does not emit. Validation also pins the category rows to the scoring configuration.

Regenerate REPORT_TEMPLATE.md and the blank template PDF after editing the JSON:

    python -m app.reports.template
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.errors import ConfigError
from app.models.company import FIELD_SPECS
from app.models.scoring import READINESS_CATEGORIES, ReadinessLevel

if TYPE_CHECKING:  # pragma: no cover
    from app.reports.report_data import ReportData

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_PATH = TEMPLATE_DIR / "readiness_report.template.json"
MARKDOWN_NAME = "REPORT_TEMPLATE.md"
BLANK_PDF_NAME = "readiness_report_template.pdf"

#: Sections the renderer has builders for, in page order.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "header", "overall_readiness", "investor_assessment", "category_scores", "fundraising_snapshot",
    "strongest_areas", "weakest_areas", "investor_concerns", "questions_to_practice", "recommended_next_steps",
    "footer",
)
#: Field slots the renderer binds, per section.
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "header": frozenset({"report_title", "report_date", "mode_note", "company", "sector", "stage", "raise"}),
    "overall_readiness": frozenset({"overall_score", "readiness_level", "interview_facts", "provisional_note",
                                    "ended_early_note"}),
    "investor_assessment": frozenset({"investor_assessment", "highest_risk_topic"}),
    "footer": frozenset({"confidentiality_notice", "document_title", "page_number", "compiled_line", "logo"}),
}
LIST_COMPONENTS = frozenset({"bullet_list", "numbered_list"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentSpec(_Model):
    title: str
    footer_title: str
    filename_pattern: str
    page_size: str
    orientation: Literal["portrait", "landscape"]
    max_pages: int = Field(ge=1)
    margins_in: dict[Literal["left", "right", "top", "bottom"], float]
    font: str
    type_scale_pt: dict[Literal["title", "section_header", "body", "score", "footer"], float]
    palette: dict[str, str]
    date_format: str
    money_format: str
    percent_format: str
    missing_value_marker: str
    not_assessed_marker: str
    no_score_marker: str
    confidentiality_notice: str


class FitLevelSpec(_Model):
    body_pt: float = Field(gt=5, lt=14)
    max_list_items: int = Field(ge=1)
    text_scale: float = Field(gt=0, le=1)


class Band(_Model):
    min: int
    max: int
    label: str
    color: str | None = None


class VocabularyEntry(_Model):
    value: str
    meaning: str


class Placement(_Model):
    row: int = Field(ge=1)
    column: Literal["full", "left", "right"]
    width_fraction: float = Field(gt=0, le=1)
    stack: int = 0


class TemplateField(_Model):
    key: str
    label: str | None = None
    type: str
    source: str | None = None
    format: str | None = None
    value: str | None = None
    values: list[str] | None = None
    placeholder: str | None = None
    max_chars: int | None = Field(default=None, ge=1)
    empty_text: str | None = None
    required: bool = False
    rule: str | None = None


class SnapshotRow(_Model):
    key: str
    label: str
    source: str
    format: str
    placeholder: str
    alternate_labels: dict[str, str] = Field(default_factory=dict)
    rule: str | None = None

    def options(self) -> list[str]:
        return [option.strip() for option in self.source.split("|")]


class CategoryRow(_Model):
    key: str
    label: str
    weight: int = Field(ge=0)
    topics: list[str]


class TemplateSection(_Model):
    id: str
    order: int
    heading: str | None = None
    component: str
    placement: Placement
    description: str
    source: str | None = None
    rule: str | None = None
    fields: list[TemplateField] = Field(default_factory=list)
    snapshot_rows: list[SnapshotRow] = Field(default_factory=list)
    category_rows: list[CategoryRow] = Field(default_factory=list)
    min_items: int | None = Field(default=None, ge=0)
    max_items: int | None = Field(default=None, ge=1)
    item_pattern: str | None = None
    item_placeholder: str | None = None
    item_max_chars: int | None = Field(default=None, ge=1)
    empty_text: str | None = None

    def field(self, key: str) -> TemplateField:
        for item in self.fields:
            if item.key == key:
                return item
        raise ConfigError(f"Report template section '{self.id}' has no field '{key}'.")


class ReportTemplate(_Model):
    template_name: str
    template_version: str
    description: str
    document: DocumentSpec
    fit_ladder: list[FitLevelSpec] = Field(min_length=1)
    layout_sketch: list[str]
    score_colors: list[Band]
    readiness_levels: list[Band]
    data_status_vocabulary: list[VocabularyEntry]
    claim_type_vocabulary: list[VocabularyEntry]
    analysis_rules: list[str]
    sections: list[TemplateSection]

    @model_validator(mode="after")
    def _consistent_with_the_application(self) -> ReportTemplate:
        problems: list[str] = []
        ids = [s.id for s in self.sections]
        if missing := [s for s in REQUIRED_SECTIONS if s not in ids]:
            problems.append(f"missing sections: {', '.join(missing)}")
        if unknown := [s for s in ids if s not in REQUIRED_SECTIONS]:
            problems.append(f"sections the renderer cannot draw: {', '.join(unknown)}")
        if len(set(ids)) != len(ids):
            problems.append("section ids must be unique")
        orders = [s.order for s in self.sections]
        if len(set(orders)) != len(orders):
            problems.append("section orders must be unique")

        by_id = {s.id: s for s in self.sections}
        for section_id, keys in REQUIRED_FIELDS.items():
            if section_id in by_id and (absent := keys - {f.key for f in by_id[section_id].fields}):
                problems.append(f"section '{section_id}' is missing fields: {', '.join(sorted(absent))}")
        for section in self.sections:
            if section.component in LIST_COMPONENTS and (section.max_items is None or section.item_max_chars is None):
                problems.append(f"list section '{section.id}' needs max_items and item_max_chars")

        snapshot = by_id.get("fundraising_snapshot")
        if snapshot is not None:
            if not snapshot.snapshot_rows:
                problems.append("fundraising_snapshot needs snapshot_rows")
            for row in snapshot.snapshot_rows:
                for option in row.options():
                    name = option.removeprefix("profile.").removesuffix("[0]")
                    if not option.startswith("profile.") or name not in FIELD_SPECS:
                        problems.append(f"snapshot row '{row.key}' binds to unknown profile field '{option}'")

        categories = by_id.get("category_scores")
        if categories is not None:
            template_rows = [(r.key, r.label, r.weight, tuple(r.topics)) for r in categories.category_rows]
            expected = [(rc.key, rc.label, rc.weight, rc.categories) for rc in READINESS_CATEGORIES]
            if template_rows != expected:
                problems.append("category_rows must match READINESS_CATEGORIES in app/models/scoring.py "
                                "(keys, labels, weights, topics and order)")

        if [band.label for band in self.readiness_levels] != [level.value for level in ReadinessLevel]:
            problems.append("readiness_levels must match ReadinessLevel in app/models/scoring.py")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def section(self, section_id: str) -> TemplateSection:
        for section in self.sections:
            if section.id == section_id:
                return section
        raise ConfigError(f"Report template has no section '{section_id}'.")

    def heading(self, section_id: str) -> str:
        return (self.section(section_id).heading or "").upper()

    def ordered_sections(self) -> list[TemplateSection]:
        return sorted(self.sections, key=lambda s: s.order)

    def filename(self, company_slug: str) -> str:
        return self.document.filename_pattern.format(company_slug=company_slug)


def load_template(path: Path | None = None) -> ReportTemplate:
    return _load(str(path or TEMPLATE_PATH))


@lru_cache(maxsize=8)
def _load(path: str) -> ReportTemplate:
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"The report template {file.name} could not be read ({exc.__class__.__name__}).") from exc
    try:
        return ReportTemplate.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'template'}: {err['msg']}" for err in exc.errors())
        raise ConfigError(f"The report template {file.name} is invalid: {details}") from exc


# --- Blank template rendering --------------------------------------------------------------

def placeholder_report_data(template: ReportTemplate | None = None) -> ReportData:
    """Report data made only of the template's placeholders, for the blank template PDF."""
    from app.models.scoring import AreaSummary
    from app.reports.report_data import ReportData

    t = template or load_template()
    header, panel, assessment = t.section("header"), t.section("overall_readiness"), t.section("investor_assessment")

    def placeholder(section: TemplateSection, key: str) -> str:
        return section.field(key).placeholder or ""

    def items(section_id: str) -> list[str]:
        section = t.section(section_id)
        return [section.item_placeholder or ""] * (section.max_items or 1)

    return ReportData(
        company=placeholder(header, "company"), sector=placeholder(header, "sector"),
        stage=placeholder(header, "stage"), raise_text=placeholder(header, "raise"),
        generated_on=placeholder(header, "report_date"), overall_score=None,
        level=placeholder(panel, "readiness_level"), provisional=True, coverage_pct=0, questions_answered=0,
        average_answer_score=None, ended_early=True,
        investor_assessment=placeholder(assessment, "investor_assessment"),
        category_scores=[(row.label, None) for row in t.section("category_scores").category_rows],
        strongest=[AreaSummary(label=label) for label in items("strongest_areas")],
        weakest=[AreaSummary(label=label) for label in items("weakest_areas")],
        concerns=items("investor_concerns"), practice_questions=items("questions_to_practice"),
        next_steps=items("recommended_next_steps"),
        snapshot=[(row.label, row.placeholder) for row in t.section("fundraising_snapshot").snapshot_rows],
        highest_risk_topic=placeholder(assessment, "highest_risk_topic"), mode_note=placeholder(header, "mode_note"),
        title=t.document.title, score_placeholder=placeholder(panel, "overall_score"),
        facts_placeholder=placeholder(panel, "interview_facts"),
        category_placeholder=t.section("category_scores").item_placeholder,
    )


def render_template_pdf(template: ReportTemplate | None = None) -> bytes:
    from app.reports.pdf_generator import generate_pdf

    t = template or load_template()
    return generate_pdf(placeholder_report_data(t), template=t)


# --- Markdown specification ----------------------------------------------------------------

def _cell(value: Any) -> str:
    if isinstance(value, bool):
        value = "yes" if value else None
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = "; ".join(f"{k}: {v}" for k, v in value.items())
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: Any) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return [*lines, ""]


def _field_value(field: TemplateField) -> str | None:
    parts = [field.value, field.format, " / ".join(field.values) if field.values else None]
    return " · ".join(p for p in parts if p) or None


def render_markdown(template: ReportTemplate | None = None) -> str:
    t = template or load_template()
    d = t.document
    lines = [
        f"# {t.template_name} — Document Template", "",
        f"Template version {t.template_version} · generated from "
        "`app/reports/templates/readiness_report.template.json` — do not edit this file by hand.", "",
        t.description, "",
        "## Document", "",
        *_table(["Property", "Value"], [
            ("Title", d.title), ("Footer title", d.footer_title), ("Filename pattern", f"`{d.filename_pattern}`"),
            ("Page size", d.page_size), ("Orientation", d.orientation), ("Max pages", d.max_pages),
            ("Margins (in)", d.margins_in), ("Font", d.font), ("Type scale (pt)", d.type_scale_pt),
            ("Date format", d.date_format), ("Money format", d.money_format), ("Percent format", d.percent_format),
            ("Missing value marker", d.missing_value_marker), ("Not assessed marker", d.not_assessed_marker),
            ("No score marker", d.no_score_marker), ("Confidentiality notice", d.confidentiality_notice),
        ]),
        "## Palette", "",
        *_table(["Name", "Hex"], [(name, f"`{hex_value}`") for name, hex_value in d.palette.items()]),
        "## Page layout", "", "```", *t.layout_sketch, "```", "",
        "## Fit-to-one-page ladder", "",
        "The report is rendered at each level in turn until it fits on one page.", "",
        *_table(["Level", "Body (pt)", "Max list items", "Text length scale"],
                [(i, level.body_pt, level.max_list_items, level.text_scale) for i, level in enumerate(t.fit_ladder, 1)]),
        "## Sections", "",
    ]
    for section in t.ordered_sections():
        placement = section.placement
        where = f"row {placement.row}, {placement.column}, width {placement.width_fraction:g}"
        if placement.stack:
            where += f", stack position {placement.stack}"
        properties: list[tuple[str, Any]] = [
            ("Section id", f"`{section.id}`"), ("Heading", section.heading.upper() if section.heading else None),
            ("Component", section.component), ("Placement", where), ("Source", f"`{section.source}`" if section.source else None),
        ]
        if section.max_items is not None:
            properties.append(("Items", f"{section.min_items or 0}–{section.max_items}"))
        properties += [("Item pattern", f"`{section.item_pattern}`" if section.item_pattern else None),
                       ("Item placeholder", section.item_placeholder), ("Item max characters", section.item_max_chars),
                       ("Empty text", section.empty_text), ("Rule", section.rule)]
        lines += [f"### {section.order}. {section.heading or section.id.replace('_', ' ').title()}", "",
                  section.description, "",
                  *_table(["Property", "Value"], [(k, v) for k, v in properties if v not in (None, "")])]
        if section.fields:
            lines += ["**Fields**", "", *_table(
                ["Key", "Label", "Type", "Source", "Value / format", "Max chars", "Placeholder", "Empty text", "Required", "Rule"],
                [(f"`{f.key}`", f.label, f.type, f"`{f.source}`" if f.source else None, _field_value(f), f.max_chars,
                  f.placeholder, f.empty_text, f.required, f.rule) for f in section.fields])]
        if section.snapshot_rows:
            lines += ["**Rows**", "", *_table(
                ["Key", "Label", "Source", "Format", "Placeholder", "Alternate labels", "Rule"],
                [(f"`{r.key}`", r.label, f"`{r.source}`", r.format, r.placeholder, r.alternate_labels, r.rule)
                 for r in section.snapshot_rows])]
        if section.category_rows:
            total = sum(r.weight for r in section.category_rows)
            lines += [f"**Rows** (weights sum to {total})", "", *_table(
                ["Key", "Label", "Weight", "Interview topics"],
                [(f"`{r.key}`", r.label, r.weight, r.topics) for r in section.category_rows])]
    lines += [
        "## Score colours", "",
        *_table(["Score", "Label", "Colour"], [(f"{b.min}–{b.max}", b.label, f"`{b.color}`") for b in t.score_colors]),
        "## Readiness classification", "",
        *_table(["Score", "Classification"], [(f"{b.min}–{b.max}", b.label) for b in t.readiness_levels]),
        "## Data status vocabulary", "",
        *_table(["Status", "Meaning"], [(f"`{v.value}`", v.meaning) for v in t.data_status_vocabulary]),
        "## Claim type vocabulary", "",
        *_table(["Claim type", "Meaning"], [(f"`{v.value}`", v.meaning) for v in t.claim_type_vocabulary]),
        "## Analysis rules", "",
        *[f"{i}. {rule}" for i, rule in enumerate(t.analysis_rules, 1)],
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_template_artifacts(directory: Path = TEMPLATE_DIR, template: ReportTemplate | None = None) -> list[Path]:
    """Write REPORT_TEMPLATE.md and the blank placeholder PDF next to the template JSON."""
    t = template or load_template()
    directory.mkdir(parents=True, exist_ok=True)
    markdown = directory / MARKDOWN_NAME
    markdown.write_text(render_markdown(t), encoding="utf-8", newline="\n")
    pdf = directory / BLANK_PDF_NAME
    pdf.write_bytes(render_template_pdf(t))
    return [markdown, pdf]


if __name__ == "__main__":  # pragma: no cover
    for written in write_template_artifacts():
        print(f"wrote {written}")

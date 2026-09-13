"""Report data: everything the one-page PDF shows, assembled and formatted in Python.

Labels, snapshot rows and their bindings, length limits and empty-value text come from the report
template (``templates/readiness_report.template.json``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.errors import SessionError
from app.models.company import CompanyProfile, DataStatus
from app.models.scoring import AreaSummary
from app.reports.template import ReportTemplate, SnapshotRow, load_template
from app.utils.text import truncate

if TYPE_CHECKING:  # pragma: no cover
    from app.session import InterviewSession


@dataclass
class ReportData:
    company: str
    sector: str
    stage: str
    raise_text: str
    generated_on: str
    overall_score: int | None
    level: str
    provisional: bool
    coverage_pct: int
    questions_answered: int
    average_answer_score: float | None
    ended_early: bool
    investor_assessment: str
    category_scores: list[tuple[str, float | None]]
    strongest: list[AreaSummary]
    weakest: list[AreaSummary]
    concerns: list[str]
    practice_questions: list[str]
    next_steps: list[str]
    snapshot: list[tuple[str, str]]
    highest_risk_topic: str | None = None
    mode_note: str = ""
    #: Empty means the template's document title.
    title: str = ""
    #: Placeholder text used only when rendering the blank template.
    score_placeholder: str | None = None
    facts_placeholder: str | None = None
    category_placeholder: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def report_filename(profile: CompanyProfile | None, extension: str = "pdf",
                    template: ReportTemplate | None = None) -> str:
    """Download/attachment filename from the template pattern, e.g. Fundraising_Interview_Readiness_Acme.pdf."""
    company = profile.display("company_name") if profile else ""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", company).strip("_")[:40] or "Company"
    stem = (template or load_template()).filename(slug).rsplit(".", 1)[0]
    return f"{stem}.{extension}"


def _suffixed(profile_value: str, status: DataStatus) -> str:
    suffix = {DataStatus.CALCULATED: " (calculated)", DataStatus.INFERRED: " (inferred)"}.get(status, "")
    return profile_value + suffix


def snapshot_row(profile: CompanyProfile, row: SnapshotRow, *, missing: str, max_chars: int) -> tuple[str, str]:
    """Resolve a template row: the first bound profile field that has a value, labelled accordingly."""
    for option in row.options():
        name = option.removeprefix("profile.")
        first_item = name.endswith("[0]")
        fv = profile.get(name.removesuffix("[0]"))
        if fv is None or fv.value is None:
            continue
        label = row.alternate_labels.get(option, row.label)
        if first_item:
            values = fv.value if isinstance(fv.value, list) else [fv.value]
            if not values:
                continue
            return label, _suffixed(truncate(str(values[0]), max_chars), fv.status)
        if not fv.display:
            continue
        return label, _suffixed(truncate(fv.display, max_chars), fv.status)
    return row.label, missing


def fundraising_snapshot(profile: CompanyProfile, template: ReportTemplate | None = None) -> list[tuple[str, str]]:
    t = template or load_template()
    section = t.section("fundraising_snapshot")
    missing = section.empty_text or t.document.missing_value_marker
    limit = section.item_max_chars or 90
    return [snapshot_row(profile, row, missing=missing, max_chars=limit) for row in section.snapshot_rows]


def build_report_data(session: InterviewSession, *, today: datetime | None = None,
                      template: ReportTemplate | None = None) -> ReportData:
    if session.assessment is None or session.profile is None or session.interview is None:
        raise SessionError("The interview has not been completed yet.")
    t = template or load_template()
    profile, assessment, state = session.profile, session.assessment, session.interview
    readiness = assessment.readiness
    header = t.section("header")
    missing = t.document.missing_value_marker

    def header_value(key: str, field_name: str) -> str:
        spec = header.field(key)
        return truncate(profile.display(field_name) or spec.empty_text or missing, spec.max_chars or 60)

    mode_values = header.field("mode_note").values or ["AI-assisted interview", "Heuristic scoring (AI processing off)"]
    date = (today or datetime.now(UTC)).strftime("%B %d, %Y").replace(" 0", " ")
    return ReportData(
        company=header_value("company", "company_name"),
        sector=header_value("sector", "sector"),
        stage=header_value("stage", "stage"),
        raise_text=header_value("raise", "raise_amount"),
        generated_on=date,
        overall_score=readiness.overall_score,
        level=readiness.level.value if readiness.level else (t.section("overall_readiness").field("readiness_level").empty_text or ""),
        provisional=readiness.provisional,
        coverage_pct=readiness.coverage_pct,
        questions_answered=readiness.questions_answered,
        average_answer_score=readiness.average_answer_score,
        ended_early=state.ended_early,
        investor_assessment=assessment.investor_assessment,
        category_scores=[(c.label, c.score) for c in readiness.category_scores],
        strongest=assessment.strongest_areas[: t.section("strongest_areas").max_items],
        weakest=assessment.weakest_areas[: t.section("weakest_areas").max_items],
        concerns=assessment.investor_concerns[: t.section("investor_concerns").max_items],
        practice_questions=assessment.questions_to_practice[: t.section("questions_to_practice").max_items],
        next_steps=assessment.next_steps[: t.section("recommended_next_steps").max_items],
        snapshot=fundraising_snapshot(profile, t),
        highest_risk_topic=readiness.highest_risk_topic,
        mode_note=mode_values[0] if session.mode == "ai" else mode_values[-1],
        title=t.document.title,
    )

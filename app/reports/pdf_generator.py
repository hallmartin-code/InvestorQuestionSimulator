"""One-page PDF: FUNDRAISING INTERVIEW READINESS REPORT (US Letter, reportlab).

The structure comes from the report template (``templates/readiness_report.template.json``): title,
section headings, field labels, item and length limits, empty-value text, column widths, margins, type
scale, fit ladder and footer text. This module only draws what the template describes.

Layout fitting is deterministic: the report is rendered at each level of the template's fit ladder
(font size, item counts, text length) until it fits on one page. If even the tightest level
overflows, the multi-page render is returned and a warning is logged.
"""

from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.doctemplate import LayoutError
from reportlab.platypus.flowables import HRFlowable

from app.errors import ConfigError, ReportError
from app.models.scoring import AreaSummary
from app.reports.charts import CategoryScoreChart
from app.reports.report_data import ReportData
from app.reports.template import FitLevelSpec, ReportTemplate, load_template
from app.reports.theme import GREY, INK, LIGHT_GREY, NAVY, PANEL, RULE, font, logo_path, score_color
from app.utils.logging import get_logger
from app.utils.text import truncate

LOGGER = get_logger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = letter
GUTTER = 14
SNAPSHOT_LABEL_WIDTH = 86
LOGO_SIZE_IN = (0.67, 0.25)


class _Page:
    def __init__(self, template: ReportTemplate) -> None:
        margins = template.document.margins_in
        self.left = margins["left"] * inch
        self.right = margins["right"] * inch
        self.top = margins["top"] * inch
        self.bottom = margins["bottom"] * inch
        self.content_width = PAGE_WIDTH - self.left - self.right

    def width(self, fraction: float) -> float:
        return self.content_width * fraction


class _Styles:
    def __init__(self, level: FitLevelSpec, template: ReportTemplate) -> None:
        scale = template.document.type_scale_pt
        regular, semibold, bold = font("OpenSans"), font("OpenSans-SemiBold"), font("OpenSans-Bold")
        body = level.body_pt
        leading = body * 1.28
        self.title = ParagraphStyle("title", fontName=bold, fontSize=scale["title"], leading=scale["title"] * 1.18,
                                    textColor=NAVY)
        self.date = ParagraphStyle("date", fontName=regular, fontSize=7.5, leading=9, textColor=GREY, alignment=TA_RIGHT)
        self.meta = ParagraphStyle("meta", fontName=regular, fontSize=body + 0.5, leading=leading + 1, textColor=INK)
        self.section = ParagraphStyle("section", fontName=bold, fontSize=scale["section_header"],
                                      leading=scale["section_header"] * 1.2, textColor=NAVY, spaceBefore=2, spaceAfter=2.5)
        self.body = ParagraphStyle("body", fontName=regular, fontSize=body, leading=leading, textColor=INK)
        self.small = ParagraphStyle("small", fontName=regular, fontSize=body - 1, leading=(body - 1) * 1.3, textColor=GREY)
        self.bullet = ParagraphStyle("bullet", parent=self.body, leftIndent=9, bulletIndent=0, spaceAfter=1.5)
        self.score = ParagraphStyle("score", fontName=bold, fontSize=scale["score"], leading=scale["score"] * 1.07,
                                    textColor=NAVY, spaceAfter=scale["score"] * 0.15)
        self.level = ParagraphStyle("level", fontName=bold, fontSize=body + 0.5, leading=leading + 1)
        self.cell_label = ParagraphStyle("cell_label", fontName=semibold, fontSize=body - 0.5,
                                         leading=(body - 0.5) * 1.25, textColor=GREY)
        self.cell_value = ParagraphStyle("cell_value", fontName=regular, fontSize=body - 0.5,
                                         leading=(body - 0.5) * 1.25, textColor=INK)


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _limit(chars: int | None, level: FitLevelSpec) -> int:
    return max(40, int((chars or 200) * level.text_scale))


def _section(heading: str, styles: _Styles) -> list[Flowable]:
    return [Paragraph(escape(heading), styles.section),
            HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=0, spaceAfter=3)]


def _list(items: list[str], styles: _Styles, *, limit: int, empty: str, numbered: bool) -> list[Flowable]:
    if not items:
        return [_p(empty, styles.small)]
    return [Paragraph(escape(truncate(item, limit)), styles.bullet, bulletText=f"{n}." if numbered else "•")
            for n, item in enumerate(items, 1)]


def _columns(left: list[Flowable], right: list[Flowable], *, left_width: float, page: _Page) -> Table:
    right_width = page.content_width - left_width - GUTTER
    table = Table([[left, "", right]], colWidths=[left_width, GUTTER, right_width])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _area_items(areas: list[AreaSummary], limit: int) -> list[str]:
    items = []
    for area in areas:
        score = f" — {area.score:.0f}/100" if area.score is not None else ""
        note = f": {area.note}" if area.note else ""
        items.append(truncate(f"{area.label}{score}{note}", limit))
    return items


def _list_section(section_id: str, items: list[str], data_styles: _Styles, level: FitLevelSpec,
                  template: ReportTemplate) -> list[Flowable]:
    section = template.section(section_id)
    count = min(level.max_list_items, section.max_items or level.max_list_items)
    return [*_section(template.heading(section_id), data_styles),
            *_list(items[:count], data_styles, limit=_limit(section.item_max_chars, level),
                   empty=section.empty_text or "", numbered=section.component == "numbered_list")]


def _story(data: ReportData, level: FitLevelSpec, styles: _Styles, template: ReportTemplate, page: _Page) -> list[Flowable]:
    t = template
    header = t.section("header")
    panel = t.section("overall_readiness")
    assessment_section = t.section("investor_assessment")
    story: list[Flowable] = []

    # Header.
    title = data.title or header.field("report_title").value or t.document.title
    top = Table([[Paragraph(escape(title), styles.title),
                  Paragraph(f"{escape(data.generated_on)}<br/>{escape(data.mode_note)}", styles.date)]],
                colWidths=[page.content_width * 0.74, page.content_width * 0.26])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    story.append(top)
    separator = " &nbsp;&nbsp;·&nbsp;&nbsp; "
    meta = separator.join(f"<b>{escape(header.field(key).label or key)}:</b> {escape(value)}" for key, value in
                          (("company", data.company), ("sector", data.sector), ("stage", data.stage),
                           ("raise", data.raise_text)))
    story.append(Paragraph(meta, styles.meta))
    story.append(HRFlowable(width="100%", thickness=1.2, color=NAVY, spaceBefore=4, spaceAfter=7))

    # Overall readiness panel and investor assessment.
    if data.overall_score is not None:
        score_text = str(data.overall_score)
    else:
        score_text = data.score_placeholder or panel.field("overall_score").empty_text or t.document.no_score_marker
    level_style = ParagraphStyle("level_c", parent=styles.level, textColor=score_color(data.overall_score))
    if data.facts_placeholder:
        facts = data.facts_placeholder
    else:
        parts = [f"{data.questions_answered} answers", f"{data.coverage_pct}% of weight assessed"]
        if data.average_answer_score is not None:
            parts.append(f"avg {data.average_answer_score:g}/20 per answer")
        facts = " · ".join(parts)
    score_block: list[Flowable] = [
        Paragraph(f"{escape(score_text)}<font size='12' color='#555555'> / 100</font>", styles.score),
        Paragraph(escape(data.level), level_style),
        Spacer(1, 2),
        _p(facts, styles.small),
    ]
    if data.provisional:
        score_block.append(_p(panel.field("provisional_note").value or "", styles.small))
    if data.ended_early:
        score_block.append(_p(panel.field("ended_early_note").value or "", styles.small))
    panel_width = page.width(panel.placement.width_fraction)
    score_panel = Table([[score_block]], colWidths=[panel_width])
    score_panel.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("LEFTPADDING", (0, 0), (-1, -1), 9),
                                     ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 6),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    assessment_field = assessment_section.field("investor_assessment")
    assessment = [*_section(t.heading("investor_assessment"), styles),
                  _p(truncate(data.investor_assessment, _limit(assessment_field.max_chars, level)), styles.body)]
    if data.highest_risk_topic:
        label = assessment_section.field("highest_risk_topic").label or "Highest-risk topic"
        assessment += [Spacer(1, 2),
                       Paragraph(f"<b>{escape(label)}:</b> {escape(data.highest_risk_topic)}", styles.body)]
    story.append(_columns([score_panel], assessment, left_width=panel_width, page=page))
    story.append(Spacer(1, 8))

    # Category scores and fundraising snapshot.
    categories = t.section("category_scores")
    chart_width = page.width(categories.placement.width_fraction)
    chart = CategoryScoreChart(data.category_scores, chart_width, row_height=11.0 if level.body_pt >= 8 else 10.2,
                               font_size=min(7.5, level.body_pt - 0.5),
                               empty_text=data.category_placeholder or categories.empty_text or t.document.not_assessed_marker)
    snapshot_section = t.section("fundraising_snapshot")
    snapshot_rows = [[_p(label, styles.cell_label),
                      _p(truncate(value, _limit(snapshot_section.item_max_chars, level)), styles.cell_value)]
                     for label, value in data.snapshot]
    snapshot_width = page.content_width - chart_width - GUTTER
    snapshot = Table(snapshot_rows, colWidths=[SNAPSHOT_LABEL_WIDTH, snapshot_width - SNAPSHOT_LABEL_WIDTH])
    snapshot.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 1.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2), ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE),
    ]))
    story.append(_columns([*_section(t.heading("category_scores"), styles), chart],
                          [*_section(t.heading("fundraising_snapshot"), styles), snapshot],
                          left_width=chart_width, page=page))
    story.append(Spacer(1, 6))

    # Strongest and weakest areas | investor concerns.
    strongest, weakest = t.section("strongest_areas"), t.section("weakest_areas")
    areas = [*_list_section("strongest_areas", _area_items(data.strongest, _limit(strongest.item_max_chars, level)),
                            styles, level, t),
             Spacer(1, 4),
             *_list_section("weakest_areas", _area_items(data.weakest, _limit(weakest.item_max_chars, level)),
                            styles, level, t)]
    concerns = _list_section("investor_concerns", data.concerns, styles, level, t)
    story.append(_columns(areas, concerns, left_width=page.width(strongest.placement.width_fraction) - GUTTER / 2,
                          page=page))
    story.append(Spacer(1, 6))

    # Questions to practice | recommended next steps.
    practice = _list_section("questions_to_practice", data.practice_questions, styles, level, t)
    steps = _list_section("recommended_next_steps", data.next_steps, styles, level, t)
    left_width = page.width(t.section("questions_to_practice").placement.width_fraction) - GUTTER / 2
    story.append(_columns(practice, steps, left_width=left_width, page=page))
    return story


def _footer(template: ReportTemplate, page: _Page, date: str):
    footer = template.section("footer")
    notice = footer.field("confidentiality_notice").value or template.document.confidentiality_notice
    document_title = footer.field("document_title").value or template.document.footer_title
    compiled = (footer.field("compiled_line").format or "Compiled on {date}").format(date=date)
    size = template.document.type_scale_pt["footer"]

    def draw(canvas, doc) -> None:  # noqa: ANN001 - reportlab callback signature
        canvas.saveState()
        regular = font("OpenSans")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(page.left, page.bottom - 12, PAGE_WIDTH - page.right, page.bottom - 12)
        canvas.setFont(regular, size)
        canvas.setFillColor(LIGHT_GREY)
        canvas.drawCentredString(PAGE_WIDTH / 2, page.bottom - 22, notice)

        # TEN Capital footer: [Document Title]  [PAGE#]  Compiled on [DATE] by TEN Capital Network  [logo]
        parts = [document_title, str(doc.page), compiled]
        gap = 14
        logo = logo_path()
        logo_w, logo_h = (LOGO_SIZE_IN[0] * inch, LOGO_SIZE_IN[1] * inch) if logo else (0, 0)
        widths = [stringWidth(p, regular, size) for p in parts]
        x = (PAGE_WIDTH - (sum(widths) + gap * len(parts) + logo_w)) / 2
        y = page.bottom - 36
        canvas.setFillColor(GREY)
        for text, width in zip(parts, widths, strict=True):
            canvas.drawString(x, y, text)
            x += width + gap
        if logo:
            try:
                canvas.drawImage(str(logo), x, y - 6, width=logo_w, height=logo_h, mask="auto", preserveAspectRatio=True)
            except Exception:  # noqa: BLE001 - a bad logo file must not stop the report
                LOGGER.warning("Could not draw the footer logo.")
        canvas.restoreState()

    return draw


def _render(data: ReportData, level: FitLevelSpec, template: ReportTemplate) -> tuple[bytes, int]:
    page = _Page(template)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=page.left, rightMargin=page.right,
                            topMargin=page.top, bottomMargin=page.bottom,
                            title=f"{template.document.footer_title} — {data.company}", author="TEN Capital Network",
                            subject="Fundraising interview readiness assessment",
                            creator="TEN Capital Interview Simulator")
    footer = _footer(template, page, data.generated_on)
    doc.build(_story(data, level, _Styles(level, template), template, page), onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue(), doc.page


def generate_pdf(data: ReportData, *, template: ReportTemplate | None = None) -> bytes:
    try:
        t = template or load_template()
    except ConfigError as exc:
        raise ReportError("The report template is invalid, so the PDF could not be generated. "
                          "Your results are still available on screen.") from exc
    fallback: bytes | None = None
    last_error: Exception | None = None
    for level in t.fit_ladder:
        try:
            pdf, pages = _render(data, level, t)
        except LayoutError as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("PDF rendering failed")
            raise ReportError("The PDF report could not be generated. Your results are still available on screen.") from exc
        if pages <= t.document.max_pages:
            return pdf
        fallback = pdf
    if fallback is not None:
        LOGGER.warning("Report did not fit on %d page(s) at the tightest layout", t.document.max_pages)
        return fallback
    raise ReportError("The PDF report could not be laid out. Your results are still available on screen.") from last_error

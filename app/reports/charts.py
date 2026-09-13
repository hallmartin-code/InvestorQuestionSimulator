"""Compact horizontal category-score chart drawn directly with reportlab primitives."""

from __future__ import annotations

from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable

from app.reports.theme import GREY, INK, LIGHT_GREY, RULE, TRACK, font, score_color


class CategoryScoreChart(Flowable):
    """One row per category: label, bar on a 0-100 track, value (``n/a`` when not assessed)."""

    def __init__(self, rows: list[tuple[str, float | None]], width: float, *, row_height: float = 11.5,
                 font_size: float = 7.5, empty_text: str = "n/a") -> None:
        super().__init__()
        self.rows = rows
        self.empty_text = empty_text
        self.width = width
        self.row_height = row_height
        self.font_size = font_size
        self.height = row_height * len(rows) + 8

    def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:  # noqa: ARG002
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        regular, bold = font("OpenSans"), font("OpenSans-SemiBold")
        label_width = max(stringWidth(label, regular, self.font_size) for label, _ in self.rows) + 6
        value_width = max(stringWidth("100", bold, self.font_size), stringWidth(self.empty_text, regular, self.font_size)) + 4
        track_x = label_width
        track_width = max(20.0, self.width - label_width - value_width - 4)
        bar_height = self.row_height - 4.5

        for index, (label, score) in enumerate(self.rows):
            y = self.height - 8 - (index + 1) * self.row_height + 2.5
            canvas.setFillColor(INK if score is not None else LIGHT_GREY)
            canvas.setFont(regular, self.font_size)
            canvas.drawString(0, y + 1.5, label)
            canvas.setFillColor(TRACK)
            canvas.rect(track_x, y, track_width, bar_height, stroke=0, fill=1)
            if score is not None:
                canvas.setFillColor(score_color(score))
                canvas.rect(track_x, y, track_width * max(0.0, min(score, 100.0)) / 100, bar_height, stroke=0, fill=1)
                canvas.setFillColor(INK)
                canvas.setFont(bold, self.font_size)
                canvas.drawRightString(self.width, y + 1.5, f"{score:.0f}")
            else:
                canvas.setFillColor(LIGHT_GREY)
                canvas.setFont(regular, self.font_size - 0.5)
                canvas.drawRightString(self.width, y + 1.5, self.empty_text)

        # Guides at the 60 and 80 readiness thresholds, with a small scale.
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        canvas.setDash(1, 1.5)
        bottom = self.height - 8 - len(self.rows) * self.row_height + 2
        for mark in (60, 80):
            x = track_x + track_width * mark / 100
            canvas.line(x, bottom, x, self.height - 6)
        canvas.setDash()
        canvas.setFillColor(GREY)
        canvas.setFont(regular, 5.5)
        for mark in (0, 60, 80, 100):
            canvas.drawCentredString(track_x + track_width * mark / 100, self.height - 5, str(mark))

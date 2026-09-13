"""TEN Capital house style for the PDF: palette, score colors and Open Sans registration.

Open Sans static TTFs (SIL OFL) are bundled in ``app/assets/fonts``. If they are missing the
renderer falls back to Helvetica - the report still renders, just off-brand.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import Color, HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.config import FONT_DIR, LOGO_PATH
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

NAVY = HexColor("#1F3864")
BLUE = HexColor("#2E75B6")
INK = HexColor("#222222")
GREY = HexColor("#555555")
LIGHT_GREY = HexColor("#8A8A8A")
RULE = HexColor("#D9D9D9")
TRACK = HexColor("#EEF1F5")
PANEL = HexColor("#F4F6F9")
GREEN = HexColor("#2E7D32")
AMBER = HexColor("#B26A00")
RED = HexColor("#8C1D18")

FONT_FILES = {
    "OpenSans": "OpenSans-Regular.ttf",
    "OpenSans-SemiBold": "OpenSans-SemiBold.ttf",
    "OpenSans-Bold": "OpenSans-Bold.ttf",
    "OpenSans-Italic": "OpenSans-Italic.ttf",
}
FALLBACKS = {
    "OpenSans": "Helvetica",
    "OpenSans-SemiBold": "Helvetica-Bold",
    "OpenSans-Bold": "Helvetica-Bold",
    "OpenSans-Italic": "Helvetica-Oblique",
}

_resolved: dict[str, str] | None = None


def register_fonts() -> dict[str, str]:
    global _resolved
    if _resolved is not None:
        return _resolved
    resolved: dict[str, str] = {}
    for name, filename in FONT_FILES.items():
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
            resolved[name] = name
        except Exception:  # noqa: BLE001 - a missing font must not stop a report
            LOGGER.warning("Font %s unavailable; using %s", filename, FALLBACKS[name])
            resolved[name] = FALLBACKS[name]
    if resolved["OpenSans"] == "OpenSans":
        try:
            pdfmetrics.registerFontFamily("OpenSans", normal="OpenSans", bold=resolved["OpenSans-Bold"],
                                          italic=resolved["OpenSans-Italic"], boldItalic=resolved["OpenSans-Bold"])
        except Exception:  # noqa: BLE001
            LOGGER.debug("Could not register the Open Sans family mapping.")
    _resolved = resolved
    return resolved


def font(name: str) -> str:
    return register_fonts().get(name, FALLBACKS.get(name, "Helvetica"))


def logo_path() -> Path | None:
    return LOGO_PATH if LOGO_PATH.is_file() else None


def score_color(score: float | None) -> Color:
    if score is None:
        return LIGHT_GREY
    if score >= 80:
        return GREEN
    if score >= 70:
        return BLUE
    if score >= 60:
        return AMBER
    return RED

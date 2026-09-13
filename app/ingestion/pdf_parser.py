"""PDF ingestion: page-by-page text and tables with PyMuPDF, preserving page numbers."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pymupdf

from app.errors import IngestError
from app.ingestion.models import DeckDocument, DeckPage
from app.utils.logging import get_logger
from app.utils.text import normalize_text

LOGGER = get_logger(__name__)

#: Table detection is comparatively slow; skip it on very long documents.
MAX_TABLE_PAGES = 60
#: Rasterization settings for the vision fallback on scanned decks.
RASTER_DPI = 110
MAX_IMAGE_LONG_EDGE = 1400


def _first_line(text: str) -> str | None:
    for line in text.split("\n"):
        if line.strip():
            return line.strip()[:200]
    return None


def _page_tables(page: pymupdf.Page) -> list[str]:
    rows: list[str] = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):  # PyMuPDF prints an advisory on first use
            finder = page.find_tables()
    except Exception:  # noqa: BLE001 - table detection is best-effort
        return rows
    for table in finder.tables:
        try:
            data = table.extract()
        except Exception:  # noqa: BLE001
            continue
        for row in data:
            cells = [" ".join(str(c).split()) for c in row if c not in (None, "")]
            if len(cells) >= 2:
                rows.append(" | ".join(cells))
    return rows


def parse_pdf(path: Path, *, display_name: str | None = None) -> DeckDocument:
    """Read a PDF into a DeckDocument; raises IngestError with an actionable message."""
    name = display_name or path.name
    try:
        # Open from memory: no file handle is held, so the parse workspace can always be deleted.
        document = pymupdf.open(stream=path.read_bytes(), filetype="pdf")
    except Exception as exc:  # PyMuPDF raises generic errors for malformed files
        raise IngestError(
            f"{name} could not be opened as a PDF. The file may be corrupt or truncated; "
            "re-export it and try again."
        ) from exc

    with document:
        if document.needs_pass:
            raise IngestError(f"{name} is password-protected. Remove the password and re-upload.")
        if document.page_count == 0:
            raise IngestError(f"{name} contains no pages.")

        pages: list[DeckPage] = []
        failed: list[int] = []
        for number in range(1, document.page_count + 1):
            try:
                page = document[number - 1]
                text = normalize_text(page.get_text("text"))
                tables = _page_tables(page) if document.page_count <= MAX_TABLE_PAGES else []
            except Exception:  # noqa: BLE001 - one bad page must not sink the deck
                failed.append(number)
                text, tables = "", []
            tables = [row for row in tables if row not in text]
            pages.append(DeckPage(number=number, title=_first_line(text), text=text, tables=tables))

    doc = DeckDocument(filename=name, source_format="pdf", pages=pages)
    if failed:
        doc.warnings.append(
            f"{len(failed)} page(s) could not be read and were skipped: {', '.join(map(str, failed[:10]))}."
        )
    LOGGER.info("Parsed PDF: %d pages, %d low-text pages", doc.page_count, len(doc.low_text_pages))
    return doc


def render_pages_png(data: bytes, page_numbers: list[int], *, max_pages: int = 15) -> list[tuple[int, bytes]]:
    """Rasterize selected pages (from in-memory PDF bytes) for vision extraction of scanned decks."""
    images: list[tuple[int, bytes]] = []
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return images
    with document:
        for number in page_numbers[:max_pages]:
            if not 1 <= number <= document.page_count:
                continue
            page = document[number - 1]
            zoom = RASTER_DPI / 72.0
            long_edge = max(page.rect.width, page.rect.height)
            if long_edge > 0:
                zoom = min(zoom, MAX_IMAGE_LONG_EDGE / long_edge)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            images.append((number, bytes(pixmap.tobytes("png"))))
    return images

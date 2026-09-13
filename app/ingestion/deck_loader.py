"""Entry point for deck ingestion: validation, private parsing workspace, quality warnings.

The uploaded bytes are written to a randomly named, owner-only directory under the restricted
upload root, under a fixed filename (never the user-supplied one), parsed, and deleted before
this function returns - whether parsing succeeded or not. Nothing about the deck is logged.
"""

from __future__ import annotations

from app.config import Settings
from app.errors import IngestError
from app.ingestion.file_validator import validate_deck_upload
from app.ingestion.models import DeckDocument
from app.ingestion.pdf_parser import parse_pdf
from app.ingestion.ppt_parser import parse_legacy_ppt, parse_pptx
from app.utils.logging import get_logger
from app.utils.security import private_workspace

LOGGER = get_logger(__name__)

MIN_USABLE_CHARS = 200


def load_deck(filename: str | None, data: bytes, *, content_type: str | None, settings: Settings) -> DeckDocument:
    upload = validate_deck_upload(filename, data, content_type=content_type, max_bytes=settings.max_upload_bytes)
    with private_workspace(settings.upload_root()) as workspace:
        path = workspace / f"deck.{upload.format}"
        path.write_bytes(data)
        try:
            if upload.format == "pdf":
                doc = parse_pdf(path, display_name=upload.safe_name)
            elif upload.format == "pptx":
                doc = parse_pptx(path, display_name=upload.safe_name)
            else:
                doc = parse_legacy_ppt(path, display_name=upload.safe_name)
        except IngestError:
            raise
        except Exception as exc:  # noqa: BLE001 - parser libraries raise arbitrary errors on hostile input
            LOGGER.warning("Deck parser failed with %s", exc.__class__.__name__)
            raise IngestError(
                f"{upload.safe_name} could not be read. Re-export the deck as a standard PDF and try again."
            ) from exc
    add_quality_warnings(doc)
    return doc


def add_quality_warnings(doc: DeckDocument) -> None:
    if doc.is_scanned:
        doc.warnings.append(
            f"Little or no extractable text: {len(doc.low_text_pages)} of {doc.page_count} slides are image-only. "
            "The deck appears to be scanned or exported as images. With AI processing enabled, slide images are "
            "read by the model (lower confidence); otherwise enter key company details on the review screen."
        )
    elif doc.low_text_pages:
        shown = ", ".join(map(str, doc.low_text_pages[:12]))
        doc.warnings.append(
            f"{len(doc.low_text_pages)} slide(s) have no extractable text ({shown}); charts or images on them "
            "were not read."
        )
    if doc.total_chars < MIN_USABLE_CHARS and not doc.is_scanned:
        doc.warnings.append("Very little text was extracted from this deck, so the interview will rely on "
                            "the company details you provide.")

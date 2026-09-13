from __future__ import annotations

import os
import time

import pytest

from app.errors import IngestError, UnsupportedFormatError, UploadRejectedError
from app.ingestion import ppt_parser
from app.ingestion.deck_loader import load_deck
from app.ingestion.file_validator import OLE_MAGIC, validate_deck_upload
from app.utils.security import purge_stale_files, sanitize_filename


def _load(settings, path, name=None, content_type=None):
    return load_deck(name or path.name, path.read_bytes(), content_type=content_type, settings=settings)


def test_pdf_ingestion_preserves_pages_and_text(settings, fixture_files):
    doc = _load(settings, fixture_files["sample_pdf"], content_type="application/pdf")
    assert doc.source_format == "pdf"
    assert doc.page_count == 12
    assert "$1.2M ARR" in doc.page_text(5)
    assert doc.page(4).title == "Market Opportunity"
    assert not doc.is_scanned
    assert doc.warnings == []


def test_pptx_ingestion_reads_tables_and_speaker_notes(settings, fixture_files):
    doc = _load(settings, fixture_files["sample_pptx"],
                content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert doc.source_format == "pptx"
    assert doc.unit == "Slide"
    assert doc.page_count == 13
    assert "Lead investor conversations" in (doc.page(10).notes or "")
    assert "CAC | $18K" in doc.page(13).tables


def test_llm_context_has_slide_markers(settings, fixture_files):
    doc = _load(settings, fixture_files["sample_pdf"])
    context, warnings = doc.llm_context()
    assert "=== Slide 10: The Ask ===" in context
    assert warnings == []
    _short, cut = doc.llm_context(max_chars=300)
    assert cut and "only slides" in cut[0]


def test_unsupported_extension_is_rejected(settings):
    with pytest.raises(UnsupportedFormatError, match="unsupported file type"):
        validate_deck_upload("notes.docx", b"PK\x03\x04data", content_type=None, max_bytes=10_000)


def test_text_file_renamed_to_pdf_is_rejected(settings):
    with pytest.raises(UnsupportedFormatError, match="not a readable PDF or PowerPoint"):
        validate_deck_upload("deck.pdf", b"hello, not a pdf", content_type="application/pdf", max_bytes=10_000)


def test_extension_must_match_contents(fixture_files):
    data = fixture_files["sample_pdf"].read_bytes()
    with pytest.raises(UnsupportedFormatError, match="contents are PDF"):
        validate_deck_upload("deck.pptx", data, content_type=None, max_bytes=len(data) + 1)


def test_declared_mime_type_must_match(fixture_files):
    data = fixture_files["sample_pdf"].read_bytes()
    with pytest.raises(UnsupportedFormatError, match="text/html"):
        validate_deck_upload("deck.pdf", data, content_type="text/html", max_bytes=len(data) + 1)
    ok = validate_deck_upload("deck.pdf", data, content_type="application/octet-stream", max_bytes=len(data) + 1)
    assert ok.format == "pdf"


def test_oversized_and_empty_uploads_are_rejected(fixture_files):
    data = fixture_files["sample_pdf"].read_bytes()
    with pytest.raises(UploadRejectedError, match="upload limit"):
        validate_deck_upload("deck.pdf", data, content_type=None, max_bytes=100)
    with pytest.raises(UploadRejectedError, match="empty"):
        validate_deck_upload("deck.pdf", b"", content_type=None, max_bytes=100)


def test_corrupted_pdf_fails_gracefully(settings, fixture_files):
    with pytest.raises(IngestError, match="could not be opened|could not be read"):
        _load(settings, fixture_files["corrupted_pdf"])


def test_corrupted_pptx_fails_gracefully(settings, fixture_files):
    with pytest.raises(UploadRejectedError, match="not a valid PPTX"):
        _load(settings, fixture_files["corrupted_pptx"])


def test_non_powerpoint_zip_is_rejected(settings, fixture_files):
    with pytest.raises(UnsupportedFormatError, match="not a PowerPoint presentation"):
        _load(settings, fixture_files["disguised_docx"])


def test_legacy_ppt_without_libreoffice_gives_conversion_instructions(settings, monkeypatch):
    monkeypatch.setattr(ppt_parser, "find_soffice", lambda: None)
    data = OLE_MAGIC + b"\x00" * 2048
    with pytest.raises(UnsupportedFormatError, match="LibreOffice"):
        load_deck("old_deck.ppt", data, content_type="application/vnd.ms-powerpoint", settings=settings)


def test_scanned_pdf_is_flagged(settings, fixture_files):
    doc = _load(settings, fixture_files["scanned_pdf"])
    assert doc.is_scanned
    assert any("scanned" in w for w in doc.warnings)


def test_filename_is_sanitized(settings, fixture_files):
    assert sanitize_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert sanitize_filename("C:\\Users\\x\\My Deck (final).pdf") == "My_Deck_final_.pdf"
    assert sanitize_filename("") == "upload"
    doc = load_deck("../../secret/Deck Q3.pdf", fixture_files["sample_pdf"].read_bytes(), content_type=None,
                    settings=settings)
    assert doc.filename == "Deck_Q3.pdf"


def test_parse_workspace_is_removed_after_success_and_failure(settings, fixture_files):
    root = settings.upload_root()
    _load(settings, fixture_files["sample_pdf"])
    assert list(root.iterdir()) == []
    with pytest.raises(IngestError):
        _load(settings, fixture_files["corrupted_pdf"])
    assert list(root.iterdir()) == []


def test_purge_removes_only_stale_entries(tmp_path):
    stale = tmp_path / "deck-old"
    stale.mkdir()
    fresh = tmp_path / "deck-new"
    fresh.mkdir()
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert purge_stale_files(tmp_path, 3600) == 1
    assert not stale.exists() and fresh.exists()

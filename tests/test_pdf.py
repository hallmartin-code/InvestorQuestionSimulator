from __future__ import annotations

import pymupdf
import pytest

from app.errors import ReportError, SessionError
from app.reports import pdf_generator
from app.reports.pdf_generator import generate_pdf
from app.reports.report_data import build_report_data
from tests.conftest import run_interview, upload


def pdf_text(data: bytes) -> tuple[int, str]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc.page_count, "\n".join(page.get_text() for page in doc)


def test_report_is_one_page_with_required_sections(completed_session, service):
    data = service.report_pdf(completed_session)
    assert data.startswith(b"%PDF")
    pages, text = pdf_text(data)
    assert pages == 1
    for expected in ("FUNDRAISING INTERVIEW READINESS REPORT", "CareLoop Health", "/ 100", "INVESTOR ASSESSMENT",
                     "CATEGORY SCORES", "STRONGEST AREAS", "WEAKEST AREAS", "INVESTOR CONCERNS",
                     "QUESTIONS TO PRACTICE", "RECOMMENDED NEXT STEPS", "FUNDRAISING SNAPSHOT", "$4M", "$1.5M",
                     "Confidential – for recipients only", "Compiled on", "TEN Capital Network"):
        assert expected in text, expected
    assert service.report_pdf(completed_session) is data  # cached for the session


def test_long_content_still_fits_one_page(completed_session):
    data = build_report_data(completed_session)
    data.investor_assessment = "A very detailed assessment sentence that keeps going. " * 40
    data.concerns = ["A long investor concern with a lot of supporting explanation and detail. " * 6] * 5
    data.practice_questions = ["A long practice question that tests every assumption in the plan? " * 5] * 3
    data.next_steps = ["A long next step with several sub-actions and deliverables to prepare. " * 5] * 3
    pages, _ = pdf_text(generate_pdf(data))
    assert pages == 1


def test_incomplete_interview_renders_with_unassessed_categories(service, session, fixture_files):
    upload(service, session, fixture_files, key="sparse_pdf")
    run_interview(service, session, limit=1)
    service.finish_interview(session)
    pages, text = pdf_text(service.report_pdf(session))
    assert pages == 1
    assert "n/a" in text and "Not provided" in text and "Provisional" in text


def test_report_requires_a_finished_interview(service, session, fixture_files):
    upload(service, session, fixture_files)
    with pytest.raises(SessionError):
        service.report_pdf(session)
    with pytest.raises(SessionError):
        build_report_data(session)


def test_rendering_failure_raises_report_error(completed_session, monkeypatch):
    def broken(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(pdf_generator, "_render", broken)
    with pytest.raises(ReportError, match="still available on screen"):
        generate_pdf(build_report_data(completed_session))

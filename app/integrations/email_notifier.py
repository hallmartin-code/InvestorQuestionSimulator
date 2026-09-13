"""Automated results email via Resend.

Every finished interview is emailed to the internal TEN Capital inbox (``EMAIL_TO``) with a summary of
the assessment and the one-page PDF report. Rules:

* A notification failure never affects the founder's results. Failures are returned as an
  ``EmailResult``, logged by error class only, and shown on the results page.
* Recipients come only from configuration, never from anything a user enters.
* The founder's name and email are included only with marketing consent. The session JSON (answers,
  evidence map, findings) is attached only with data-storage consent. With
  ``EMAIL_REQUIRES_STORAGE_CONSENT=true`` no email is sent at all without data-storage consent.
* Every user-derived value is HTML-escaped; the send carries an idempotency key so a retry cannot
  deliver the same results twice.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

import httpx

from app import __version__
from app.config import Settings
from app.models.analysis import IssueTag
from app.models.analytics import EmailResult
from app.reports.report_data import fundraising_snapshot
from app.utils.logging import get_logger
from app.utils.text import truncate

if TYPE_CHECKING:  # pragma: no cover
    from app.session import InterviewSession

LOGGER = get_logger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
MAX_ATTACHMENTS_BYTES = 15 * 1024 * 1024
NAVY = "#1F3864"
SCORE_COLOURS = ((80, "#2E7D32"), (70, "#2E75B6"), (60, "#B26A00"), (0, "#8C1D18"))


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _score_colour(score: float | None) -> str:
    if score is None:
        return "#8A8A8A"
    return next(colour for floor, colour in SCORE_COLOURS if score >= floor)


def _company(session: InterviewSession) -> str:
    return session.profile.company_name if session.profile else "Unknown company"


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def subject_for(session: InterviewSession) -> str:
    readiness = session.assessment.readiness if session.assessment else None
    score = f"{readiness.overall_score}/100" if readiness and readiness.overall_score is not None else "not scored"
    level = readiness.level.value if readiness and readiness.level else "NOT ASSESSED"
    flag = " (provisional)" if readiness and readiness.provisional else ""
    return truncate(_one_line(f"[Interview Readiness] {_company(session)} — {score} {level}{flag}"), 190)


def _founder_contact(session: InterviewSession) -> str:
    if not session.consent.marketing_consent:
        return "Withheld (no follow-up consent)"
    parts = [session.user.name or "", f"<{session.user.email}>" if session.user.email else ""]
    return _one_line(" ".join(parts)) or "Not provided"


def _open_findings(session: InterviewSession, limit: int = 5) -> list[str]:
    if session.gaps is None:
        return []
    resolved = set(session.interview.resolved_issue_ids) if session.interview else set()
    findings = [i for i in session.gaps.concerns()
                if i.issue_id not in resolved and i.tag in (IssueTag.RED_FLAG, IssueTag.INCONSISTENCY, IssueTag.EVIDENCE_GAP)]
    return [f"[{i.tag.value}] {i.title}: {i.detail}" for i in findings[:limit]]


def _facts(session: InterviewSession) -> str:
    readiness = session.assessment.readiness
    parts = [f"{readiness.questions_answered} answers", f"{readiness.coverage_pct}% of weight assessed"]
    if readiness.average_answer_score is not None:
        parts.append(f"avg {readiness.average_answer_score:g}/20 per answer")
    if session.interview and session.interview.ended_early:
        parts.append("ended early")
    return " · ".join(parts)


def _descriptor(session: InterviewSession) -> str:
    profile = session.profile
    if profile is None:
        return ""
    raise_text = profile.display("raise_amount")
    parts = [profile.display("sector"), profile.display("stage"), f"Raise {raise_text}" if raise_text else ""]
    return " · ".join(p for p in parts if p) or "Sector, stage and raise not provided"


def _completed(session: InterviewSession) -> str:
    moment = session.interview_finished_at or datetime.now(UTC)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# --- Bodies ------------------------------------------------------------------------------------

def render_text(session: InterviewSession, attachments: list[str]) -> str:
    assessment = session.assessment
    readiness = assessment.readiness
    score = f"{readiness.overall_score}/100" if readiness.overall_score is not None else "not scored"
    lines = [
        f"FUNDRAISING INTERVIEW READINESS — {_company(session)}",
        _descriptor(session),
        f"Completed {_completed(session)} · {'AI-assisted' if session.mode == 'ai' else 'heuristic mode'} · "
        f"session {session.analytics_id}",
        "",
        f"READINESS: {score} — {readiness.level.value if readiness.level else 'NOT ASSESSED'}"
        + (" (provisional)" if readiness.provisional else ""),
        _facts(session),
        f"Highest-risk topic: {readiness.highest_risk_topic or '—'}",
        "",
        "INVESTOR ASSESSMENT",
        assessment.investor_assessment,
        "",
        "CATEGORY SCORES",
        *[f"  {c.label:<28} {'n/a' if c.score is None else f'{c.score:.0f}':>4}   (weight {c.weight})"
          for c in readiness.category_scores],
        "",
        "STRONGEST AREAS",
        *[f"  - {a.label}" + (f" — {a.score:.0f}/100" if a.score is not None else "") for a in assessment.strongest_areas],
        "WEAKEST AREAS",
        *[f"  - {a.label}" + (f" — {a.score:.0f}/100" if a.score is not None else "") for a in assessment.weakest_areas],
        "",
        "INVESTOR CONCERNS",
        *[f"  - {c}" for c in assessment.investor_concerns],
        "",
        "PRACTICE PLAN",
    ]
    for n, priority in enumerate(assessment.practice_plan, 1):
        score_text = f"{priority.score:.0f}/100" if priority.score is not None else "not assessed"
        lines += [f"  {n}. {priority.category} ({score_text}): {priority.recommended_action}",
                  f"     Practice: \"{priority.practice_question}\""]
    lines += ["", "RECOMMENDED NEXT STEPS", *[f"  {n}. {s}" for n, s in enumerate(assessment.next_steps, 1)]]
    if session.profile is not None:
        lines += ["", "FUNDRAISING SNAPSHOT", *[f"  {label:<18} {value}" for label, value in fundraising_snapshot(session.profile)]]
    if session.interview is not None:
        lines += ["", "INTERVIEW"]
        for n, response in enumerate(session.interview.responses, 1):
            follow = " follow-up" if response.is_follow_up else ""
            lines.append(f"  Q{n} [{response.category.label}, L{int(response.difficulty)}{follow}] "
                         f"{response.scores.total}/20 — {truncate(response.question, 150)}")
    if findings := _open_findings(session):
        lines += ["", "OPEN DECK FINDINGS", *[f"  - {truncate(f, 220)}" for f in findings]]
    consent = session.consent
    lines += [
        "", "CONSENT",
        f"  AI processing: {_yes(consent.ai_processing_consent)} · Analytics: {_yes(consent.analytics_consent)} · "
        f"Data storage: {_yes(consent.data_storage_consent)} · Follow-up: {_yes(consent.marketing_consent)}",
        f"  Founder contact: {_founder_contact(session)}",
    ]
    if attachments:
        lines += ["", "ATTACHED", *[f"  {name}" for name in attachments]]
    lines += ["", f"Confidential – for recipients only. TEN Capital Fundraising Interview Simulator v{__version__}."]
    return "\n".join(lines)


def _section(title: str) -> str:
    return (f'<h3 style="margin:20px 0 6px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;'
            f'color:{NAVY};border-bottom:1px solid #d9d9d9;padding-bottom:3px">{escape(title)}</h3>')


def _list(items: list[str], ordered: bool = False) -> str:
    if not items:
        return '<p style="margin:0;color:#8a8a8a;font-size:13px">None recorded.</p>'
    tag = "ol" if ordered else "ul"
    rows = "".join(f'<li style="margin:0 0 4px">{escape(item)}</li>' for item in items)
    return f'<{tag} style="margin:0;padding-left:20px;font-size:13px;line-height:1.45">{rows}</{tag}>'


def render_html(session: InterviewSession, attachments: list[str]) -> str:
    assessment = session.assessment
    readiness = assessment.readiness
    score = readiness.overall_score
    colour = _score_colour(score)
    level = readiness.level.value if readiness.level else "NOT ASSESSED"

    categories = "".join(
        f'<tr><td style="padding:3px 10px 3px 0;font-size:12px;color:{"#222" if c.score is not None else "#8a8a8a"}">'
        f'{escape(c.label)} <span style="color:#8a8a8a">({c.weight})</span></td>'
        f'<td style="padding:3px 0;width:180px"><div style="background:#eef1f5;height:9px;border-radius:2px">'
        f'<div style="background:{_score_colour(c.score)};height:9px;width:{0 if c.score is None else round(c.score)}%;'
        f'border-radius:2px"></div></div></td>'
        f'<td style="padding:3px 0 3px 10px;font-size:12px;font-weight:700;text-align:right">'
        f'{"n/a" if c.score is None else f"{c.score:.0f}"}</td></tr>'
        for c in readiness.category_scores
    )
    practice = "".join(
        f'<tr><td style="padding:8px 0;border-top:1px solid #eee;font-size:13px">'
        f'<strong>{escape(p.category)}</strong> <span style="color:#8a8a8a">'
        f'{"not assessed" if p.score is None else f"{p.score:.0f}/100"}</span><br>'
        f'<span style="color:#555">{escape(p.weakness)}</span><br>'
        f'<strong>Action:</strong> {escape(p.recommended_action)}<br>'
        f'<em>Practice: “{escape(p.practice_question)}”</em></td></tr>'
        for p in assessment.practice_plan
    )
    snapshot = "".join(
        f'<tr><td style="padding:3px 12px 3px 0;font-size:12px;color:#6b6b6b;white-space:nowrap">{escape(label)}</td>'
        f'<td style="padding:3px 0;font-size:13px">{escape(value)}</td></tr>'
        for label, value in (fundraising_snapshot(session.profile) if session.profile else [])
    )
    interview = "".join(
        f'<tr><td style="padding:5px 8px 5px 0;border-top:1px solid #eee;font-size:12px;color:#6b6b6b;white-space:nowrap">'
        f'Q{n} · {escape(r.category.label)}{" · follow-up" if r.is_follow_up else ""}</td>'
        f'<td style="padding:5px 8px;border-top:1px solid #eee;font-size:12px">{escape(truncate(r.question, 200))}</td>'
        f'<td style="padding:5px 0;border-top:1px solid #eee;font-size:12px;font-weight:700;text-align:right;'
        f'color:{_score_colour(r.scores.percentage)}">{r.scores.total}/20</td></tr>'
        for n, r in enumerate(session.interview.responses if session.interview else [], 1)
    )
    consent = session.consent
    notes = []
    if readiness.provisional:
        notes.append("Provisional score: interview coverage was limited.")
    if session.interview and session.interview.ended_early:
        notes.append("The founder ended the interview before all planned questions.")

    return f"""<!doctype html>
<html><body style="margin:0;background:#f4f6f9;font-family:'Open Sans',Segoe UI,Arial,sans-serif;color:#222">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:20px 0">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #dde2e8;border-radius:8px">
<tr><td style="padding:22px 26px 8px">
  <p style="margin:0;font-size:11px;letter-spacing:.08em;color:#2E75B6;font-weight:700">FUNDRAISING INTERVIEW READINESS</p>
  <h1 style="margin:4px 0 2px;font-size:22px;color:{NAVY}">{escape(_company(session))}</h1>
  <p style="margin:0;font-size:13px;color:#555">{escape(_descriptor(session))}</p>
  <p style="margin:4px 0 0;font-size:11px;color:#8a8a8a">Completed {escape(_completed(session))} ·
    {"AI-assisted" if session.mode == "ai" else "Heuristic mode"} · session {escape(session.analytics_id)}</p>
</td></tr>
<tr><td style="padding:10px 26px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;border-radius:6px">
  <tr><td style="padding:14px 18px;width:130px;vertical-align:top">
    <span style="font-size:36px;font-weight:700;color:{colour}">{"—" if score is None else score}</span>
    <span style="font-size:14px;color:#555">/ 100</span></td>
  <td style="padding:14px 18px 14px 0;vertical-align:top">
    <p style="margin:0 0 4px;font-size:14px;font-weight:700;color:{colour}">{escape(level)}</p>
    <p style="margin:0;font-size:12px;color:#555">{escape(_facts(session))}</p>
    <p style="margin:4px 0 0;font-size:12px;color:#555">Highest-risk topic: <strong>{escape(readiness.highest_risk_topic or "—")}</strong></p>
    {"".join(f'<p style="margin:4px 0 0;font-size:12px;color:#8C1D18">{escape(n)}</p>' for n in notes)}
  </td></tr></table>
</td></tr>
<tr><td style="padding:0 26px 22px">
  {_section("Investor assessment")}
  <p style="margin:0;font-size:13px;line-height:1.5">{escape(assessment.investor_assessment)}</p>
  {_section("Category scores")}
  <table role="presentation" cellpadding="0" cellspacing="0">{categories}</table>
  {_section("Strongest areas")}
  {_list([a.label + (f" — {a.score:.0f}/100" if a.score is not None else "") for a in assessment.strongest_areas])}
  {_section("Weakest areas")}
  {_list([a.label + (f" — {a.score:.0f}/100" if a.score is not None else "") for a in assessment.weakest_areas])}
  {_section("Investor concerns")}
  {_list(assessment.investor_concerns)}
  {_section("Practice plan")}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{practice}</table>
  {_section("Recommended next steps")}
  {_list(assessment.next_steps, ordered=True)}
  {_section("Fundraising snapshot")}
  <table role="presentation" cellpadding="0" cellspacing="0">{snapshot}</table>
  {_section("Interview")}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{interview}</table>
  {_section("Open deck findings")}
  {_list([truncate(f, 240) for f in _open_findings(session)])}
  {_section("Consent")}
  <p style="margin:0;font-size:12px;color:#555">AI processing: {_yes(consent.ai_processing_consent)} ·
    Analytics: {_yes(consent.analytics_consent)} · Data storage: {_yes(consent.data_storage_consent)} ·
    Follow-up: {_yes(consent.marketing_consent)}<br>Founder contact: {escape(_founder_contact(session))}</p>
  {(_section("Attached") + _list(attachments)) if attachments else ""}
</td></tr>
<tr><td style="padding:12px 26px;border-top:1px solid #eee;font-size:11px;color:#8a8a8a">
  Confidential – for recipients only · TEN Capital Fundraising Interview Simulator v{__version__}
</td></tr>
</table></td></tr></table></body></html>"""


# --- Delivery ------------------------------------------------------------------------------------

def build_payload(session: InterviewSession, settings: Settings, *, pdf: bytes | None, pdf_name: str,
                  export: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    attachments: list[dict[str, str]] = []
    names: list[str] = []
    budget = MAX_ATTACHMENTS_BYTES
    candidates: list[tuple[str, bytes]] = []
    if pdf is not None:
        candidates.append((pdf_name, pdf))
    if export is not None:
        candidates.append((pdf_name.rsplit(".", 1)[0] + "_session.json",
                           json.dumps(export, indent=2, default=str).encode("utf-8")))
    for name, content in candidates:
        if len(content) > budget:
            LOGGER.warning("Attachment %s skipped: over the email size budget", name.rsplit(".", 1)[-1])
            continue
        budget -= len(content)
        attachments.append({"filename": name, "content": base64.b64encode(content).decode("ascii")})
        names.append(name)

    payload: dict[str, Any] = {
        "from": settings.email_from,
        "to": settings.email_recipients,
        "subject": subject_for(session),
        "html": render_html(session, names),
        "text": render_text(session, names),
        "tags": [{"name": "app", "value": "interview_simulator"}],
    }
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    if attachments:
        payload["attachments"] = attachments
    return payload, names


def send_results_email(session: InterviewSession, settings: Settings, *, pdf: bytes | None, pdf_name: str,
                       export: dict[str, Any] | None, client: httpx.Client | None = None) -> EmailResult:
    """Send the results email. Never raises."""
    if not settings.email_enabled:
        return EmailResult(status="skipped", detail="Results email is turned off (EMAIL_ENABLED=false).")
    if settings.resend_api_key is None:
        return EmailResult(status="skipped", detail="Results email is not configured (RESEND_API_KEY is not set).")
    if settings.email_requires_storage_consent and not session.consent.data_storage_consent:
        return EmailResult(status="skipped", detail="Results email not sent: the founder did not grant data-storage consent.")
    if session.assessment is None:
        return EmailResult(status="skipped", detail="There are no results to email yet.")

    payload, names = build_payload(session, settings, pdf=pdf, pdf_name=pdf_name, export=export)
    recipients = list(settings.email_recipients)
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"interview-results-{session.analytics_id}",
        "User-Agent": f"ten-interview-simulator/{__version__}",
    }
    owns_client = client is None
    http = client or httpx.Client(timeout=settings.email_timeout_seconds)
    detail = "Results email could not be sent."
    try:
        for _attempt in range(2):
            try:
                response = http.post(RESEND_ENDPOINT, json=payload, headers=headers,
                                     timeout=settings.email_timeout_seconds)
            except httpx.HTTPError as exc:
                detail = f"Resend could not be reached ({exc.__class__.__name__})."
                continue
            if 200 <= response.status_code < 300:
                try:
                    message_id = response.json().get("id")
                except ValueError:
                    message_id = None
                return EmailResult(status="sent", detail=f"Results emailed to {', '.join(recipients)}.",
                                   message_id=message_id, recipients=recipients, attachments=names)
            try:
                message = _one_line(str(response.json().get("message", "")))
            except ValueError:
                message = ""
            detail = f"Resend returned HTTP {response.status_code}" + (f": {truncate(message, 200)}" if message else ".")
            if response.status_code < 500 and response.status_code != 429:
                break
    except Exception as exc:  # noqa: BLE001 - email must never break the session
        detail = f"Results email failed ({exc.__class__.__name__})."
    finally:
        if owns_client:
            http.close()
    LOGGER.warning("Results email not delivered: %s", detail)
    return EmailResult(status="failed", detail=detail, recipients=recipients, attachments=names)

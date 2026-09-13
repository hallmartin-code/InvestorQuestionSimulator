"""Jinja filters for the web templates."""

from __future__ import annotations

import re

from jinja2 import Environment
from markupsafe import Markup, escape

_PLACEHOLDER = re.compile(r"\[[^\[\]]{1,60}\]")


def score_class(score: float | None) -> str:
    if score is None:
        return "na"
    if score >= 80:
        return "good"
    if score >= 70:
        return "ok"
    if score >= 60:
        return "warn"
    return "bad"


def dimension_class(value: int) -> str:
    return score_class(value / 5 * 100)


def highlight_placeholders(text: str | None) -> Markup:
    """Escape the text, then mark bracketed placeholders so founders see what to fill in."""
    escaped = str(escape(text or ""))
    return Markup(_PLACEHOLDER.sub(lambda m: f'<span class="placeholder">{m.group(0)}</span>', escaped))


def status_label(status: str) -> str:
    return {
        "VERIFIED_FROM_DECK": "From deck",
        "USER_PROVIDED": "Founder input",
        "CALCULATED": "Calculated",
        "INFERRED": "Inferred",
        "MISSING": "Missing",
    }.get(status, status)


def register_filters(env: Environment) -> None:
    env.filters["score_class"] = score_class
    env.filters["dimension_class"] = dimension_class
    env.filters["placeholders"] = highlight_placeholders
    env.filters["status_label"] = status_label

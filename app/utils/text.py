"""Text helpers shared by ingestion, analysis, interview and reporting."""

from __future__ import annotations

import difflib
import re

_WS = re.compile("[ \t    ]+")
_BLANKS = re.compile(r"\n{3,}")
_TOKEN = re.compile(r"[a-z0-9]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

STOPWORDS = frozenset(
    """a an the and or but if of to in on for with at by from as is are was were be been being it its
    this that these those you your we our they their i me my do does did how what why who whom which
    when where will would can could should have has had not no so than then there here about into over
    under more most much many any some each per vs versus just very walk tell give us let lets me
    specifically exactly today""".split()
)


def normalize_text(raw: str) -> str:
    """Collapse horizontal whitespace and runs of blank lines, preserving line structure."""
    lines = [_WS.sub(" ", line).strip() for line in raw.replace("\r\n", "\n").split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def truncate(text: str | None, limit: int, *, ellipsis: str = "…") -> str:
    """Shorten to at most ``limit`` characters, breaking on a word boundary when possible."""
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    cut = text[: limit - len(ellipsis)]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—") + ellipsis


def first_sentence(text: str, limit: int = 240) -> str:
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()), maxsplit=1)
    return truncate(parts[0] if parts else "", limit)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def content_tokens(text: str) -> set[str]:
    """Meaningful, lightly stemmed tokens (stopwords and very short tokens removed)."""
    return {_stem(t) for t in tokens(text) if t not in STOPWORDS and (len(t) > 2 or t.isdigit())}


def jaccard(a: str, b: str) -> float:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def sequence_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, " ".join(tokens(a)), " ".join(tokens(b))).ratio()


def overlap_ratio(quote: str, text: str) -> float:
    """Share of the quote's tokens that occur in ``text`` (used to verify cited quotes)."""
    quote_tokens = set(tokens(quote))
    if not quote_tokens:
        return 0.0
    return len(quote_tokens & set(tokens(text))) / len(quote_tokens)


def numbers_in(text: str) -> set[str]:
    """Numbers in canonical form: commas removed, trailing zeros dropped ("2.40" == "2.4")."""
    found: set[str] = set()
    for raw in _NUMBER.findall((text or "").replace(",", "")):
        try:
            found.add(f"{float(raw):g}")
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
    return found

"""Parsing and formatting of money, percentages and plain numbers found in decks and forms."""

from __future__ import annotations

import re

_UNITS: dict[str, float] = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "tn": 1e12, "trillion": 1e12,
}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT = r"thousand|million|billion|trillion|mm|mn|bn|tn|[kmbt]"
_COUNT_NOUNS = r"users|customers|downloads|patients|people|units|subscribers|members|visits|transactions|clients|devices|lives|students|employees"

MONEY_RE = re.compile(
    rf"(?P<cur>[$€£])\s?(?P<num>{_NUM})(?:\s?(?P<unit>{_UNIT})\b)?"
    rf"|(?P<num2>{_NUM})\s?(?P<unit2>thousand|million|billion|trillion|mm|mn|bn|tn|[kmb])\b(?!\s*(?:{_COUNT_NOUNS}))",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s?%")
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def money_mentions(text: str) -> list[tuple[str, float, int]]:
    """Every money amount in ``text`` as (matched text, value, start offset)."""
    found: list[tuple[str, float, int]] = []
    for match in MONEY_RE.finditer(text or ""):
        num = match.group("num") or match.group("num2")
        unit = (match.group("unit") or match.group("unit2") or "").lower()
        value = _to_float(num)
        if value is None:
            continue
        value *= _UNITS.get(unit, 1.0)
        found.append((match.group(0).strip(), value, match.start()))
    return found


def parse_money(text: str | None) -> float | None:
    """"$2.4M" -> 2400000.0; "3,000,000" -> 3000000.0; unparseable -> None."""
    if text is None:
        return None
    mentions = money_mentions(str(text))
    if mentions:
        return mentions[0][1]
    stripped = str(text).strip().lstrip("$€£").strip()
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", stripped):
        return _to_float(stripped)
    return None


def parse_percent(text: str | None) -> float | None:
    if text is None:
        return None
    match = PERCENT_RE.search(str(text))
    if match:
        return float(match.group(1))
    stripped = str(text).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
        return float(stripped)
    return None


def parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    match = NUMBER_RE.search(str(text))
    return _to_float(match.group(0)) if match else None


def _trim(value: float, digits: int = 1) -> str:
    text = f"{value:,.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_money(value: float | None, currency: str = "$") -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1e9:
        return f"{sign}{currency}{_trim(amount / 1e9, 2)}B"
    if amount >= 1e6:
        return f"{sign}{currency}{_trim(amount / 1e6, 2)}M"
    if amount >= 1e3:
        return f"{sign}{currency}{_trim(amount / 1e3, 1)}K"
    return f"{sign}{currency}{_trim(amount, 2)}"


def format_percent(value: float | None) -> str:
    return "—" if value is None else f"{_trim(value, 1)}%"


def format_number(value: float | None) -> str:
    return "—" if value is None else _trim(value, 2)

"""Canonical forms for protected values, so equivalent wordings compare equal.

"five business days" and "5 working days" both become ``5 business_day``;
"annually" and "once a year" both become ``1/1 year``; "less than 10 %" and
"under ten percent" both become ``<10%``. Differences that change meaning
("5 days" vs "5 calendar days", "at least 10" vs "10") stay different.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

# ── Number words ──────────────────────────────────────────────────────

_UNITS_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALE_WORDS = {"hundred": 100, "thousand": 1000}

_UNIT_ALT = "|".join(sorted(_UNITS_WORDS, key=len, reverse=True))
_TENS_ALT = "|".join(_TENS_WORDS)
# "five", "twenty", "twenty-four", "twenty four", "one hundred", "a hundred"
NUMBER_WORDS_RE = (
    rf"(?:(?:{_TENS_ALT})(?:[- ](?:{_UNIT_ALT}))?|(?:{_UNIT_ALT}))(?:\s+(?:hundred|thousand))?"
    rf"|(?:a\s+)?(?:hundred|thousand)"
)
DIGITS_RE = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


def words_to_number(text: str) -> Optional[int]:
    tokens = re.split(r"[\s-]+", text.strip().lower())
    tokens = [t for t in tokens if t and t not in ("a", "and")]
    if not tokens:
        return None
    total, current = 0, 0
    for token in tokens:
        if token in _UNITS_WORDS:
            current += _UNITS_WORDS[token]
        elif token in _TENS_WORDS:
            current += _TENS_WORDS[token]
        elif token in _SCALE_WORDS:
            current = max(current, 1) * _SCALE_WORDS[token]
            if token == "thousand":
                total, current = total + current, 0
        else:
            return None
    return total + current


def number_value(raw: str) -> Optional[str]:
    """'1,000' → '1000', '2.50' → '2.5', '3.0' → '3', 'twenty-four' → '24', 'a'/'an' → '1'."""
    raw = raw.strip().lower()
    if raw in ("a", "an", "once", "single"):
        return "1"
    if raw == "twice":
        return "2"
    if raw == "thrice":
        return "3"
    if re.fullmatch(DIGITS_RE, raw):
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            return None
        text = format(value.normalize(), "f")
        return text
    words = words_to_number(raw)
    return str(words) if words is not None else None


# ── Time units ────────────────────────────────────────────────────────

TIME_UNIT_RE = (
    r"(?:business|working|work|calendar)\s+days?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?"
    r"|weeks?|months?|years?|quarters?"
)


def time_unit(raw: str, aliases: dict[str, str]) -> str:
    unit = re.sub(r"\s+", " ", raw.strip().lower())
    unit = re.sub(r"s$", "", unit) if unit not in ("hrs", "mins", "secs") else unit[:-1]
    unit = {"hr": "hour", "min": "minute", "sec": "second"}.get(unit, unit)
    if unit.endswith(" day"):
        qualifier = unit.split()[0]
        unit = {"business": "business_day", "working": "working_day", "work": "working_day", "calendar": "calendar_day"}[qualifier]
    return aliases.get(unit, unit)


# ── Comparison qualifiers ─────────────────────────────────────────────

_QUALIFIERS: list[tuple[str, str]] = [
    (">=", r"at\s+least|minimum(?:\s+of)?|min\.?|not\s+less\s+than|no\s+less\s+than|≥|>="),
    ("<=", r"at\s+most|maximum(?:\s+of)?|max\.?|not\s+more\s+than|no\s+more\s+than|up\s+to|not\s+(?:to\s+)?exceed(?:ing)?|≤|<="),
    (">", r"more\s+than|greater\s+than|over|above|exceeding|>"),
    ("<", r"less\s+than|fewer\s+than|below|under|<"),
]
QUALIFIER_RE = "|".join(f"(?:{pattern})" for _, pattern in _QUALIFIERS)

# Deadline wording for durations: "within 5 days" bounds the time like "<=".
DEADLINE_RE = r"within|no\s+later\s+than|not\s+later\s+than|at\s+the\s+latest|in\s+no\s+more\s+than"


def qualifier_symbol(raw: Optional[str]) -> str:
    if not raw:
        return ""
    raw = raw.strip().lower()
    for symbol, pattern in _QUALIFIERS:
        if re.fullmatch(pattern, raw):
            return symbol
    if re.fullmatch(DEADLINE_RE, raw):
        return "<="
    return ""


# ── Measurement units ─────────────────────────────────────────────────

MEASURE_UNIT_RE = (
    r"°\s?[CF]|deg(?:rees?)?\s?[CF]\b|degrees?\s+(?:celsius|fahrenheit)|µg|ug|mg|kg|g|ml|mL|µl|l|L|cm|mm|km|m|rpm|ppm|ppb"
    r"|bar|psi|kPa|Pa|kV|V|mA|A|kW|W|kHz|Hz|TB|GB|MB|kB|KB"
)


def measure_unit(raw: Optional[str]) -> str:
    if not raw:
        return ""
    unit = re.sub(r"\s+", "", raw.strip())
    low = unit.lower()
    if low.startswith("°") or low.startswith("deg"):
        return "°f" if low.endswith("f") or "fahrenheit" in low else "°c"
    if low in ("ug", "µg"):
        return "µg"
    if low in ("l", "ml", "µl"):
        return low.replace("l", "L")
    return low


# ── Dates ─────────────────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?"
    r"|nov(?:ember)?|dec(?:ember)?"
)


def month_number(raw: str) -> int:
    return MONTHS[raw.strip().lower()[:3]]


def iso_date(year: int, month: int, day: Optional[int] = None) -> Optional[str]:
    if not 1 <= month <= 12 or (day is not None and not 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day is not None else "")


# ── Frequencies ───────────────────────────────────────────────────────

def frequency(count: int, every: str, unit: str) -> str:
    """'count per every-unit', reduced: every 12 months → per year, every 3 months → per quarter."""
    n = Decimal(every)
    conversions = {("month", Decimal(12)): "year", ("month", Decimal(3)): "quarter", ("hour", Decimal(24)): "day",
                   ("day", Decimal(7)): "week", ("week", Decimal(52)): "year"}
    if (unit, n) in conversions:
        unit, n = conversions[(unit, n)], Decimal(1)
    return f"{count}/{format(n.normalize(), 'f')} {unit}"


@dataclass(frozen=True)
class PreservationConfig:
    """Equivalences that count as the same value. Defaults treat working days as business days."""

    unit_aliases: dict[str, str] = field(default_factory=lambda: {"working_day": "business_day"})

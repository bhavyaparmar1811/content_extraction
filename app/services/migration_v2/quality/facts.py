"""Protected-fact extraction: values, obligations, roles, references and terms (Phase 6).

The same functions read source units and drafted claims, so both sides are
normalized identically. Spans are claimed in priority order and blanked out,
so a date is never also read as three numbers and a document ID never as a
number:

    references → cross-reference phrases (dropped) → dates → frequencies
    → durations/deadlines → percentages → numbers with units → number words

Cross-references ("see chapter 6.3") are not protected values: the drafter
writes them as ``{{ref:...}}`` tokens and the NumberMap renumbers them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.schemas.v2 import (
    FactKind,
    Modality,
    ProtectedFacts,
    ProtectedObligation,
    ProtectedValue,
    SourceDocument,
    SourceUnit,
    UnitType,
)

from .normalize import (
    DEADLINE_RE,
    DIGITS_RE,
    MEASURE_UNIT_RE,
    MONTH_RE,
    NUMBER_WORDS_RE,
    QUALIFIER_RE,
    TIME_UNIT_RE,
    PreservationConfig,
    frequency,
    iso_date,
    measure_unit,
    month_number,
    number_value,
    qualifier_symbol,
    time_unit,
)

_REF_TOKEN = re.compile(r"\{\{ref:[^}]*\}\}")
_LIST_MARKER = re.compile(r"^\s*(?:\(?\d{1,2}[.)]|\(?[a-zA-Z][.)]|[•▪◦‣∙●○*\-–o])\s+")
_HEADING_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)+\s+")
_F = re.IGNORECASE


def _clean(text: str) -> str:
    text = _REF_TOKEN.sub(" ", text or "")
    text = text.replace(" ", " ").replace("–", "-").replace("—", "-").replace("‑", "-")
    text = _HEADING_NUMBER.sub("", text)
    return _LIST_MARKER.sub("", text)


class _Text:
    """Text with spans blanked out as they are claimed."""

    def __init__(self, text: str):
        self.text = text

    def take(self, pattern: re.Pattern) -> list[re.Match]:
        matches = list(pattern.finditer(self.text))
        for m in matches:
            self.text = self.text[: m.start()] + " " * (m.end() - m.start()) + self.text[m.end():]
        return matches


# ── References ────────────────────────────────────────────────────────

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"\bhttps?://[^\s)>\]]+", _F)
_DOC_ID = re.compile(
    r"\b(?=[A-Z0-9-]*\d)[A-Z][A-Z0-9]{0,6}(?:-[A-Z0-9]{1,10}){2,6}\b"  # BI-VQD-10095-S, SOP-QA-001
    r"|\b\d{2,4}-[A-Z]{2,6}-\d{2,6}(?:-[A-Z0-9]{1,8})*\b"  # 028-BIS-00493, 028-BIS-00535-RD00
    r"|\b(?:ISO|IEC|ICH|EN|USP|Ph\.\s?Eur\.)\s?[A-Z]?\d+[A-Z]?(?:[-:.]\d+)*\b"  # ISO 9001, ICH Q9
    r"|\b\d+\s+CFR\s+(?:Part\s+)?\d+(?:\.\d+)*\b"  # 21 CFR Part 11
)
_XREF = re.compile(
    r"\b(?:chapters?|sections?|sec\.|steps?|figures?|fig\.|tables?|appendix|appendices|annex(?:es)?|attachments?"
    r"|items?|no\.|nos\.|paragraphs?|points?|examples?)\s+\d+(?:\.\d+)*"
    r"(?:\s*(?:,|and|or|to|-)\s*\d+(?:\.\d+)*)*",
    _F,
)

# ── Dates ─────────────────────────────────────────────────────────────

_DATE_DMY = re.compile(rf"\b(\d{{1,2}})\.?\s+({MONTH_RE})\.?,?\s+(\d{{4}})\b", _F)
_DATE_MDY = re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", _F)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_NUM = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")  # day first (European documents)
_DATE_MY = re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{4}})\b", _F)

# ── Frequencies ───────────────────────────────────────────────────────

_FREQ_UNIT = r"(?:minute|hour|day|week|month|quarter|year)s?"
_FREQ_WORDS = {
    "hourly": "1/1 hour", "daily": "1/1 day", "weekly": "1/1 week", "fortnightly": "1/2 week", "monthly": "1/1 month",
    "quarterly": "1/1 quarter", "annually": "1/1 year", "annual": "1/1 year", "yearly": "1/1 year",
    "semi-annually": "2/1 year", "semiannually": "2/1 year", "semi-annual": "2/1 year", "per annum": "1/1 year",
}
_FREQ_WORD = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(_FREQ_WORDS, key=len, reverse=True)) + r")\b", _F)
_FREQ_EVERY = re.compile(rf"\b(?:every|each)\s+(?:({DIGITS_RE}|{NUMBER_WORDS_RE})\s+)?({_FREQ_UNIT})\b", _F)
_FREQ_TIMES = re.compile(
    rf"\b(once|twice|thrice|{DIGITS_RE}|{NUMBER_WORDS_RE})\s+(?:times?\s+)?(?:a|per|each|every)\s+({_FREQ_UNIT})\b", _F
)

# ── Durations, percentages, numbers ───────────────────────────────────

_NUM = rf"{DIGITS_RE}|{NUMBER_WORDS_RE}"
_DURATION = re.compile(
    rf"(?:\b({QUALIFIER_RE}|{DEADLINE_RE})\s+)?"
    rf"\b(?!an?\s+sec)({_NUM}|an?)(?:\s*(?:-|to|and)\s*({_NUM}))?\s+({TIME_UNIT_RE})\b(?!-)",
    _F,
)
_PERCENT = re.compile(
    rf"(?:\b({QUALIFIER_RE})\s+)?(?<![\w.])({_NUM})(?:\s*(?:-|to)\s*({_NUM}))?\s*(?:%|\bper\s?cent\b|\bpercent\b)",
    _F,
)
_BETWEEN = re.compile(
    rf"\bbetween\s+({DIGITS_RE})\s*({MEASURE_UNIT_RE})?\s+and\s+({DIGITS_RE})\s*({MEASURE_UNIT_RE})?(?![\w])", _F
)
_RANGE = re.compile(rf"(?<![\w.-])({DIGITS_RE})(?:\s*-\s*|\s+to\s+)({DIGITS_RE})\s*({MEASURE_UNIT_RE})?(?![\w-])")
_TIME_OF_DAY = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NUMBER = re.compile(
    rf"(?:\b({QUALIFIER_RE})\s+|(?<![\w])([<>≤≥]=?)\s*)?(?<![\w.,-])({DIGITS_RE})(?:\s*({MEASURE_UNIT_RE}))?(?![\w-]|[.,]\d)"
)
_NUMBER_WORD = re.compile(rf"(?:\b({QUALIFIER_RE})\s+)?\b({NUMBER_WORDS_RE})\b", _F)
# "one" is a number only in front of a noun-like word: not "one of", "no one", "one another".
_ONE_STOP_AFTER = {
    "of", "or", "and", "another", "by", "at", "to", "in", "on", "which", "that", "who", "must", "should", "may", "can",
    "will", "is", "are", "more", "less", "way", "time", "",
}
_ONE_STOP_BEFORE = {"no", "any", "every", "each", "some", "this", "that", "the", "which", "anyone", "someone"}


@dataclass
class Value:
    kind: FactKind
    raw: str
    normalized: str


def _qual(raw: Optional[str]) -> str:
    return qualifier_symbol(raw) if raw else ""


def extract_values(text: str, config: Optional[PreservationConfig] = None) -> list[Value]:
    """Protected values in a piece of text, with canonical forms."""
    config = config or PreservationConfig()
    t = _Text(_clean(text))
    out: list[Value] = []

    for m in t.take(_EMAIL) + t.take(_URL):
        out.append(Value(FactKind.REFERENCE, m.group(0), m.group(0).lower().rstrip(".,;")))
    for m in t.take(_DOC_ID):
        out.append(Value(FactKind.REFERENCE, m.group(0), re.sub(r"\s+", " ", m.group(0)).upper()))
    t.take(_XREF)

    for pattern, order in ((_DATE_DMY, "dmy"), (_DATE_MDY, "mdy"), (_DATE_ISO, "ymd"), (_DATE_NUM, "dmy_num")):
        for m in t.take(pattern):
            g = m.groups()
            if order == "dmy":
                iso = iso_date(int(g[2]), month_number(g[1]), int(g[0]))
            elif order == "mdy":
                iso = iso_date(int(g[2]), month_number(g[0]), int(g[1]))
            elif order == "ymd":
                iso = iso_date(int(g[0]), int(g[1]), int(g[2]))
            else:
                iso = iso_date(int(g[2]), int(g[1]), int(g[0]))
            if iso:
                out.append(Value(FactKind.DATE, m.group(0), iso))
    for m in t.take(_DATE_MY):
        if m.group(1).lower() == "may" and not m.group(0)[0].isupper():
            continue  # "may 2025" in running text is unlikely to be a date; keep it conservative
        iso = iso_date(int(m.group(2)), month_number(m.group(1)))
        if iso:
            out.append(Value(FactKind.DATE, m.group(0), iso))

    for m in t.take(_FREQ_WORD):
        out.append(Value(FactKind.FREQUENCY, m.group(0), _FREQ_WORDS[m.group(0).lower()]))
    for m in t.take(_FREQ_EVERY):
        every = number_value(m.group(1)) if m.group(1) else "1"
        if every:
            out.append(Value(FactKind.FREQUENCY, m.group(0), frequency(1, every, time_unit(m.group(2), {}))))
    for m in t.take(_FREQ_TIMES):
        count = number_value(m.group(1))
        if count:
            out.append(Value(FactKind.FREQUENCY, m.group(0), frequency(int(float(count)), "1", time_unit(m.group(2), {}))))

    for m in t.take(_DURATION):
        qual, first, second, unit = m.groups()
        kind = FactKind.DEADLINE if qual and re.fullmatch(DEADLINE_RE, qual.strip(), _F) else FactKind.DURATION
        unit_name = time_unit(unit, config.unit_aliases)
        symbol = _qual(qual)
        for raw_n in (first, second):
            n = number_value(raw_n) if raw_n else None
            if n is not None:
                out.append(Value(kind, m.group(0).strip(), f"{symbol}{n} {unit_name}"))

    for m in t.take(_PERCENT):
        qual, first, second = m.groups()
        for raw_n in (first, second):
            n = number_value(raw_n) if raw_n else None
            if n is not None:
                out.append(Value(FactKind.PERCENTAGE, m.group(0).strip(), f"{_qual(qual)}{n}%"))

    for m in t.take(_TIME_OF_DAY):
        out.append(Value(FactKind.NUMBER, m.group(0), f"{int(m.group(1)):02d}:{m.group(2)}"))
    for m in t.take(_BETWEEN):
        a, unit_a, b, unit_b = m.groups()
        unit = measure_unit(unit_b or unit_a)
        for raw_n in (a, b):
            out.append(Value(FactKind.NUMBER, m.group(0), f"{number_value(raw_n)}{' ' + unit if unit else ''}"))
    for m in t.take(_RANGE):
        a, b, unit_raw = m.groups()
        unit = measure_unit(unit_raw)
        for raw_n in (a, b):
            out.append(Value(FactKind.NUMBER, m.group(0), f"{number_value(raw_n)}{' ' + unit if unit else ''}"))
    for m in t.take(_NUMBER):
        qual_words, qual_sym, digits, unit_raw = m.groups()
        symbol = _qual(qual_words) or _qual(qual_sym)
        unit = measure_unit(unit_raw)
        out.append(Value(FactKind.NUMBER, m.group(0).strip(), f"{symbol}{number_value(digits)}{' ' + unit if unit else ''}"))

    for m in _NUMBER_WORD.finditer(t.text):
        qual, words = m.groups()
        if words.lower() == "one":
            before = t.text[: m.start(2)].split()
            after = t.text[m.end(2):].split()
            prev = before[-1].lower().strip(",;:") if before else ""
            nxt = after[0].lower().strip(",.;:") if after else ""
            if prev in _ONE_STOP_BEFORE or nxt in _ONE_STOP_AFTER:
                continue
        n = number_value(words)
        if n is not None:
            out.append(Value(FactKind.NUMBER, m.group(0).strip(), f"{_qual(qual)}{n}"))
    return out


# ── Obligations (modality) ────────────────────────────────────────────

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9\"“(])")
_MODALITY_PATTERNS: list[tuple[Modality, re.Pattern]] = [
    (Modality.PROHIBITION, re.compile(
        r"\b(?:must|shall|may)\s+not\b|\bmustn'?t\b|\bshan'?t\b|\bcannot\b|\bcan\s+not\b|\bcan'?t\b"
        r"|\b(?:is|are|be)\s+(?:strictly\s+)?(?:prohibited|forbidden)\b|\b(?:is|are|be)\s+not\s+(?:allowed|permitted)\b"
        r"|\bnot\s+(?:allowed|permitted)\s+to\b|\bnever\b|^(?:do\s+not|don'?t)\b", _F)),
    (Modality.RECOMMENDED, re.compile(
        r"\bshould(?:\s+not|n'?t)?\b|\bought\s+to\b|\b(?:is|are)\s+(?:strongly\s+)?recommended\b|\brecommends?\b"
        r"|\bit\s+is\s+advisable\b", _F)),
    (Modality.MANDATORY, re.compile(
        r"\b(?:can|may|could)\s+only\b|\bmust\b|\bshall\b|\b(?:is|are|be)\s+required\b|\brequired\s+to\b"
        r"|\b(?:has|have)\s+to\b|\bneeds?\s+to\b|\b(?:is|are)\s+(?:obliged|obligated)\s+to\b|\b(?:is|are)\s+mandatory\b"
        r"|\bmandatory\b", _F)),
    (Modality.PERMITTED, re.compile(
        r"\bmay\b|\bmight\b|\bcan\b|\bcould\b|\b(?:is|are)\s+(?:allowed|permitted)\s+to\b|\boptional(?:ly)?\b"
        r"|\bat\s+the\s+discretion\s+of\b", _F)),
    (Modality.FUTURE, re.compile(r"\bwill\b|\bwon'?t\b", _F)),
]
_MODALITY_EXCLUDE = re.compile(
    r"\b(?:if|as|where|when|whenever)\s+(?:required|needed|necessary|applicable)\b"
    r"|\b(?:can|could)\s+(?:also\s+)?be\s+(?:found|seen|located|viewed|accessed|retrieved|downloaded)\b"
    r"|\bMay\s+\d"  # the month
    , _F,
)
IMPERATIVE_VERBS = frozenset("""
    accept access acknowledge add adjust align allocate allow analyse analyze apply approve archive arrange ask assess
    assign attach audit authorize avoid back calculate calibrate cancel capture carry change check choose classify clean
    clear close collect communicate compare complete conduct configure confirm consider consult contact continue control
    copy correct create decide define delete deliver describe design destroy determine develop discuss distribute
    document download draft enable enter escalate establish estimate evaluate execute explain export file fill finalize
    follow forward generate give hand handle identify implement import include inform initiate insert inspect install
    investigate issue keep label launch list log maintain make manage mark measure monitor move name note notify obtain
    open order organize pack perform place plan prepare present print prioritize process provide publish put qualify
    raise read receive record reconcile register reject release remove repeat replace report request resolve restrict
    retain retrieve return review revise run save scan schedule select send set share ship sign specify start state stop
    store submit summarize supply support switch take tell test track train transfer translate transmit update upload
    use validate verify wait write
""".split())


def sentences(text: str) -> list[str]:
    cleaned = _clean(text).strip()
    return [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]


def sentence_modalities(sentence: str) -> set[Modality]:
    t = _Text(sentence)
    t.take(_MODALITY_EXCLUDE)
    found: set[Modality] = set()
    for modality, pattern in _MODALITY_PATTERNS:
        if t.take(pattern):
            found.add(modality)
    if _is_imperative(sentence):
        found.add(Modality.MANDATORY)  # an instruction: "Approve the deviation."
    return found


_NOUN_FOLLOWERS = {"of", "is", "are", "was", "were", "must", "shall", "should", "will", "can", "may", "has", "have", "and", "or"}


def _is_imperative(sentence: str) -> bool:
    """Starts with a base-form verb used as a command: 'Approve the record', not 'Review of the record'."""
    words = re.findall(r"[A-Za-z']+", sentence)
    if not words or not words[0][0].isupper() or words[0].lower() not in IMPERATIVE_VERBS:
        return False
    if _ROLE_PHRASE.match(sentence.lstrip(" \"'(")):
        return False  # "Process Owner approves", "Change Requester ..." name an actor
    return len(words) == 1 or words[1].lower() not in _NOUN_FOLLOWERS


def extract_obligations(text: str, unit_id: str, roles: Iterable[str] = ()) -> list[ProtectedObligation]:
    roles = list(roles)
    out = []
    for sentence in sentences(text):
        for modality in sorted(sentence_modalities(sentence), key=lambda m: m.value):
            actor = next((r for r in find_roles(sentence, roles) if r), None)
            out.append(ProtectedObligation(unit_id=unit_id, statement=sentence, modality=modality, actor=actor))
    return out


def text_modalities(texts: Iterable[str]) -> set[Modality]:
    found: set[Modality] = set()
    for text in texts:
        for sentence in sentences(text):
            found |= sentence_modalities(sentence)
    return found


# ── Terms and roles ───────────────────────────────────────────────────

_ABBR_PAREN = re.compile(r"\(([A-Z][A-Za-z0-9&]{1,9})\)")
_MINOR_WORDS = {"and", "of", "for", "the", "&", "to", "in", "on"}


def extract_terms(texts: Iterable[str]) -> dict[str, str]:
    """'Full Term (FT)' anywhere → {'Full Term': 'FT'}; the abbreviation must match the initials."""
    terms: dict[str, str] = {}
    for text in texts:
        for m in _ABBR_PAREN.finditer(text or ""):
            abbr = m.group(1)
            words = re.findall(r"[A-Za-z][\w&/-]*|&", text[: m.start()])[-10:]
            letters = [c for c in abbr if c.isalpha() and c.isupper()]
            for size in range(1, len(words) + 1):
                candidate = words[-size:]
                if candidate[0].lower() in _MINOR_WORDS:
                    continue
                initials = [w[0].upper() for w in candidate if w.lower() not in _MINOR_WORDS]
                if initials == letters:
                    terms[" ".join(candidate)] = abbr
                    break
    return terms


_ROLE_HEADS = (
    "Owner|Manager|Head|Lead|Leader|Officer|Coordinator|Approver|Reviewer|Author|Representative|Expert|Administrator"
    "|Specialist|Supervisor|Director|Auditor|Inspector|Operator|Analyst|Designee|Sponsor|Steward|Engineer|Scientist"
    "|Technician|Person|Committee|Board|Team|Department|User|Developer|Initiator|Assessor|Signatory|Requester|Requestor"
    "|Responsible|Approvers|Reviewers|Owners|Managers|Users"
)
_ROLE_PHRASE = re.compile(
    rf"\bHead\s+of\s+[A-Z][\w&/-]*(?:\s+[A-Z][\w&/-]*){{0,4}}"
    rf"|\b(?:(?:[A-Z][A-Za-z0-9&/-]*|[A-Z]{{2,}})\s+){{0,4}}(?:{_ROLE_HEADS})\b(?!\s+[A-Z][a-z])"
)
_ROLE_LEADING_NOISE = {"the", "a", "an", "each", "every", "all", "any", "if", "when", "only", "this", "that", "these", "those",
                       "our", "your", "their", "its", "his", "her", "for", "by", "to", "and", "or"}
COMMON_ROLES = ("Quality Assurance", "Quality Control", "Qualified Person")
_ROLE_TABLE_HEADING = re.compile(r"role|responsib", re.I)
_ROLE_COLUMN = re.compile(r"\b(?:roles?|functions?|positions?|who)\b", re.I)


def _strip_role(phrase: str) -> str:
    words = phrase.split()
    while words and words[0].lower() in _ROLE_LEADING_NOISE:
        words = words[1:]
    return " ".join(words).strip(" ,;:")


def find_roles(text: str, known: Iterable[str] = ()) -> list[str]:
    """Role mentions: known roles (exact, case-sensitive) plus capitalized role phrases."""
    text = _clean(text)
    spans: dict[str, list[tuple[int, int]]] = {}
    for role in set(known):
        if role:
            hits = [(m.start(), m.end()) for m in re.finditer(rf"(?<![\w-]){re.escape(role)}(?![\w-])", text)]
            if hits:
                spans[role] = hits
    for m in _ROLE_PHRASE.finditer(text):
        role = _strip_role(m.group(0))
        if role and len(role.split()) <= 6:
            start = m.start() + m.group(0).find(role)
            spans.setdefault(role, []).append((start, start + len(role)))

    def inside_longer(role: str) -> bool:
        """Every mention of ``role`` sits inside a longer role mention ('QA' in 'QA Manager')."""
        others = [s for r, hits in spans.items() if r != role and len(r) > len(role) for s in hits]
        return all(any(o[0] <= a and b <= o[1] for o in others) for a, b in spans[role])

    found = [r for r in spans if not inside_longer(r)]
    return sorted(found, key=lambda r: min(a for a, _ in spans[r]))


def extract_roles(doc: SourceDocument, terms: dict[str, str]) -> list[str]:
    """Roles from Roles & Responsibilities tables, role phrases in the text, and role-like defined terms."""
    roles: list[str] = []
    for section in doc.sections:
        if not _ROLE_TABLE_HEADING.search(section.heading):
            continue
        for unit in section.units:
            if unit.unit_type != UnitType.TABLE_ROW or unit.table_ref is None or unit.is_boilerplate:
                continue
            headers = unit.table_ref.header_cells
            if not headers or not _ROLE_COLUMN.search(headers[0]):
                continue  # e.g. a deliverables table: its first column names documents, not roles
            cells = [c for c in unit.table_ref.cells if c.text.strip()]
            if not cells or cells[0].is_header:
                continue
            first = re.sub(r"\s+", " ", cells[0].text).strip()
            if first and first.lower() not in ("role", "roles", "function", "name") and len(first.split()) <= 8:
                roles.append(first)
    for unit in doc.iter_units():
        if not unit.is_boilerplate:
            roles += find_roles(unit.text)
    for full, abbr in terms.items():
        if _ROLE_PHRASE.fullmatch(full) or full in COMMON_ROLES:
            roles += [full, abbr]
    seen: set[str] = set()
    return [r for r in roles if not (r in seen or seen.add(r))]


# ── Registry ──────────────────────────────────────────────────────────


_TOC_HEADING = re.compile(r"^\s*(?:table\s+of\s+contents?|contents)\s*$", re.I)


def protected_units(doc: SourceDocument) -> list[SourceUnit]:
    """Units whose facts are protected: everything except boilerplate (cover page, history, approvals)
    and a table of contents, whose page numbers are regenerated, not migrated."""
    return [
        u
        for section in doc.sections
        if not _TOC_HEADING.match(section.heading)
        for u in section.units
        if not u.is_boilerplate and u.text.strip()
    ]


def extract_facts(doc: SourceDocument, job_id: str, config: Optional[PreservationConfig] = None) -> ProtectedFacts:
    units = protected_units(doc)
    terms = extract_terms(u.text for u in units)
    roles = extract_roles(doc, terms)
    values: list[ProtectedValue] = []
    obligations: list[ProtectedObligation] = []
    for unit in units:
        for v in extract_values(unit.text, config):
            values.append(ProtectedValue(kind=v.kind, raw=v.raw, normalized=v.normalized, unit_id=unit.unit_id))
        obligations += [o for o in extract_obligations(unit.text, unit.unit_id, roles) if o.modality != Modality.FUTURE]
    return ProtectedFacts(job_id=job_id, values=values, obligations=obligations, approved_terms=terms, roles=roles)

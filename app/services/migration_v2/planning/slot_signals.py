"""Deterministic unit → slot signals for the Level 2 slot planner (zero tokens).

Nothing here is specific to one template. A slot's meaning is read from its own
key and instruction text: a slot whose instruction asks for "the applicable
geography" activates the ``geography`` concept, whose cue words ("world-wide",
"site", "country"...) are then looked for in the source passages. A template
whose slots activate no concept still works: passages fall back to the slot
whose instruction they resemble most, marked for the LLM to check.

Three kinds of evidence:
- text cues: concept cue words in a passage (or a group of passages);
- tables: a table goes to one slot as a whole, by its header and cells
  (abbreviation, term, role and RACI tables);
- icon rows: when the source has exactly as many icon rows as the section has
  icon slots, row *k* feeds icon slot *k* (icons are matched by position, never
  by image, as in the template).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from app.schemas.v2 import (
    AssetKind,
    ConditionalKind,
    ConditionalRegion,
    FormattingProfile,
    SourceUnit,
    TargetSection,
    TargetSlot,
    UnitType,
)

from ..quality.facts import _ROLE_COLUMN
from .signals import _cosine, tokens


@dataclass(frozen=True)
class Concept:
    name: str
    trigger: re.Pattern  # against the slot key and instruction
    cue: re.Pattern      # against passage text
    min_hits: int = 2    # distinct cue words needed to feed this slot in addition to a passage's best slot


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


TEXT_CONCEPTS: tuple[Concept, ...] = (
    Concept(
        "what", _rx(r"^what\b|\bwhat\s+(?:the\s+)?(?:document|process)\b|\bwhat\s+process\b|\bdescription\s+of\s+what\b"),
        _rx(r"\b(?:this|the\s+present|it)\s+(?:\w+\s+){0,2}(?:also\s+)?(?:describes|documents|defines|outlines|specifies"
            r"|sets\s+out|establishes|covers|explains|governs|details|summari[sz]es|regulates|serves\s+as)\b"),
        min_hits=1,
    ),
    Concept(
        "intention", _rx(r"\bintention\b|\bachieve\b|\bgoal\b|\bobjective\b"),
        _rx(r"\bin\s+order\s+to\b|\bframework\b|\bto\s+(?:ensure|retain|maintain|guarantee)\b|\bso\s+that\b|\baims?\s+to\b"
            r"|\bintended\s+to\b|\bintention\b|\bgoal\b|\bachiev\w+|\bobjective\b"),
        min_hits=1,
    ),
    Concept(
        "target_roles", _rx(r"\btarget\s+roles?\b|\broles?\b[^.]{0,40}\bfollow\b|^roles$"),
        _rx(r"\bemployees?\b|\bcontractors?\b|\bstaff\b|\bpersonnel\b|\bbinding\s+for\b|\busers?\b|\bis\s+valid\s+for\b"
            r"|\bapplicable\s+(?:to|for)\b|\bwho\s+(?:are|is)\s+involved\b|\bmust\s+follow\b|\binitiating\b"),
    ),
    Concept(
        "org_units", _rx(r"\bbusiness\s+units?\b|\bgroup\s+functions?\b|\bdepartments?\b|^units$"),
        _rx(r"\bdepartments?\b|\bdivisions?\b|\bfunctions?\b|\bbusiness\s+units?\b|\borgani[sz]ations?\b|\bdeployments?\b"
            r"|\bteams?\b|\bmanagement\b|\bnetwork\b|\bprogram(?:me)?\b"),
    ),
    Concept(
        "geography", _rx(r"\bgeograph"),
        _rx(r"\bworld-?wide\b|\bglobally\b|\bcountr(?:y|ies)\b|\bsites?\b|\bregions?\b|\br/?opus?\b|\blocal(?:ly)?\b"
            r"|\binternational(?:ly)?\b"),
    ),
    Concept(
        "processes_systems", _rx(r"\baffected\s+processes\b|\bprocesses\s*/\s*systems\b|\bsystems?\b[^.]{0,30}\bmaterials?\b"),
        _rx(r"\bprocess(?:es)?\b|\bsystems?\b|\bmaterials?\b|\bproducts?\b|\bautomation\b|\bqualifications?\b"
            r"|\blogistics\b|\bdevices?\b|\bgoods\b|\bverification\b"),
    ),
    Concept(
        "not_covered", _rx(r"\bnot\s+covered\b|\bout\s+of\s+scope\b|^not_covered$"),
        _rx(r"\bnot\s+(?:in\s+)?scope\b|\bout\s+of\s+scope\b|\bnot\s+covered\b|\bdoes\s+not\s+apply\b|\bexclud\w+"
            r"|\bnot\s+applicable\b|\bnot\s+part\s+of\b"),
        min_hits=1,
    ),
)

_RACI_CELL = re.compile(r"^[RACIX](?:\s*[&/,+]?\s*[RACIX]){0,3}\s*[\d¹²³⁴⁵⁶⁷⁸⁹⁾)]*$", re.I)


@dataclass(frozen=True)
class TableConcept:
    name: str
    trigger: re.Pattern


TABLE_CONCEPTS: tuple[TableConcept, ...] = (
    TableConcept("abbreviations", _rx(r"\babbreviations?\b|\bacronyms?\b")),
    TableConcept("terms", _rx(r"\bterms?\b|\bdefinitions?\b|\bglossary\b")),
    TableConcept("role_table", _rx(r"\broles?\b")),
    TableConcept("raci", _rx(r"\braci\b|\bto\s+indicate\b|\bsections?\(?s?\)?\s+applicable\s+to\s+each\s+role\b")),
)


def slot_text(slot: TargetSlot) -> str:
    return f"{(slot.key or '').replace('_', ' ')} {slot.instruction}"


def concept_activation(slot: TargetSlot, trigger: re.Pattern) -> float:
    """2 when the slot's key names the concept, 1 when only its instruction does, else 0."""
    key = (slot.key or "").replace("_", " ")
    if key and (trigger.search(key) or trigger.search(slot.key or "")):
        return 2.0
    return 1.0 if trigger.search(slot.instruction or "") else 0.0


def is_table_slot(slot: TargetSlot) -> bool:
    return slot.formatting_profile == FormattingProfile.TABLE


def is_callout_slot(slot: TargetSlot) -> bool:
    return slot.callout_kind is not None


def has_icon(unit: SourceUnit) -> bool:
    return any(a.kind == AssetKind.ICON for a in unit.assets)


def is_table_row(unit: SourceUnit) -> bool:
    """A row of a data table. Paragraphs inside a layout table (e.g. the icon rows of a scope box) are text."""
    return unit.table_ref is not None and unit.unit_type in (
        UnitType.TABLE_ROW, UnitType.TABLE_CELL_GROUP, UnitType.DEFINITION, UnitType.REFERENCE)


def is_text_unit(unit: SourceUnit) -> bool:
    return unit.unit_type in (UnitType.PARAGRAPH, UnitType.BULLET, UnitType.PROCEDURE_STEP, UnitType.NOTE,
                              UnitType.WARNING, UnitType.HEADING_STATEMENT)


# ── Text cues ─────────────────────────────────────────────────────────


def cue_hits(text: str, concept: Concept) -> int:
    """Distinct cue words of a concept in the text."""
    return len({m.group(0).lower() for m in concept.cue.finditer(text or "")})


@dataclass
class SlotScores:
    """Per-slot evidence for one passage or group: cue score and distinct hits per concept."""

    score: dict[str, float] = field(default_factory=dict)  # slot_id -> weighted cue score
    hits: dict[str, int] = field(default_factory=dict)      # slot_id -> distinct cue words
    strong: dict[str, bool] = field(default_factory=dict)   # slot_id -> enough hits to feed it as a second slot

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(((s, v) for s, v in self.score.items() if v > 0), key=lambda kv: kv[1], reverse=True)


def text_scores(text: str, slots: list[TargetSlot]) -> SlotScores:
    out = SlotScores()
    for slot in slots:
        best, hits, strong = 0.0, 0, False
        for concept in TEXT_CONCEPTS:
            activation = concept_activation(slot, concept.trigger)
            if not activation:
                continue
            n = cue_hits(text, concept)
            if n and activation * n > best:
                best, hits = activation * n, n
            strong = strong or (n >= concept.min_hits and activation >= 2)
        out.score[slot.slot_id], out.hits[slot.slot_id], out.strong[slot.slot_id] = best, hits, strong
    return out


def lexical_best(text: str, slots: list[TargetSlot]) -> tuple[Optional[TargetSlot], float]:
    """The slot whose key and instruction share the most words with the text (bag-of-words cosine)."""
    bag = Counter(tokens(text))
    best, best_score = None, 0.0
    for slot in slots:
        score = _cosine(bag, Counter(tokens(slot_text(slot))))
        if score > best_score:
            best, best_score = slot, score
    return best, round(best_score, 3)


# ── Tables ────────────────────────────────────────────────────────────


def table_scores(rows: list[SourceUnit], slots: list[TargetSlot]) -> dict[str, float]:
    """Score each slot for a whole table, from its first header cell and its cells."""
    first = rows[0]
    headers = first.table_ref.header_cells if first.table_ref else []
    first_header = headers[0] if headers else ""
    cells = [c.text.strip() for r in rows if r.table_ref for c in r.table_ref.cells if c.col > 0 and c.text.strip()]
    raci_share = sum(bool(_RACI_CELL.match(c)) for c in cells) / len(cells) if cells else 0.0

    evidence = {
        "abbreviations": 1.0 if re.search(r"abbreviation|acronym|short\s*form", first_header, re.I) else 0.0,
        "terms": 1.0 if re.search(r"\bterms?\b|\bexpressions?\b|\bwords?\b", first_header, re.I) else 0.0,
        "role_table": 1.0 if _ROLE_COLUMN.search(first_header) else 0.0,
        "raci": 1.5 if raci_share >= 0.3 else 0.0,
    }
    out: dict[str, float] = {}
    for slot in slots:
        out[slot.slot_id] = max(
            (concept_activation(slot, c.trigger) * evidence[c.name] for c in TABLE_CONCEPTS), default=0.0
        )
    return out


# ── Conditional regions (inline choices) ──────────────────────────────


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", (text or "").lower())


def inline_choice(unit: SourceUnit, regions: list[ConditionalRegion]) -> Optional[tuple[ConditionalRegion, Optional[str]]]:
    """The inline-choice region a short source lead-in answers, and the option it uses.

    "This SOP is applicable:" answers "This Directive/SOP/Work Instruction/Guidance is applicable:"
    with "SOP": every word of the lead-in is in the region text, and one slash option is in the lead-in.
    """
    text = (unit.text or "").strip()
    if not text.endswith(":") or len(text) > 80:
        return None
    words = _words(text)
    for region in regions:
        if region.kind != ConditionalKind.INLINE_CHOICE:
            continue
        region_words = set(_words(region.text))
        if not words or not set(words) <= region_words:
            continue
        choice = None
        for group in re.findall(r"[\w ]+(?:/[\w ]+)+", region.text):
            options = [o.strip() for o in group.split("/") if o.strip()]
            choice = next((o for o in options if re.search(rf"\b{re.escape(o.split()[-1])}\b", text, re.I)), None)
            if choice:
                # "This Directive" -> "Directive": keep only the option word(s) the lead-in uses.
                choice = next((w for w in reversed(choice.split()) if re.search(rf"\b{re.escape(w)}\b", text, re.I)), choice)
                break
        return region, choice
    return None


# ── Icon rows ─────────────────────────────────────────────────────────


def is_continuation(unit: SourceUnit) -> bool:
    """A short label line under an icon row (e.g. a list of department names), not a sentence of its own."""
    text = (unit.text or "").strip()
    return (unit.unit_type in (UnitType.PARAGRAPH, UnitType.BULLET) and not has_icon(unit) and 0 < len(text) <= 80
            and not text.endswith((".", ":", ";")))


def icon_groups(units: list[SourceUnit]) -> list[list[SourceUnit]]:
    """Units grouped into icon rows: an icon unit plus the label lines that follow it. Other units stand alone."""
    groups: list[list[SourceUnit]] = []
    open_row = False
    for unit in units:
        if has_icon(unit):
            groups.append([unit])
            open_row = True
        elif open_row and is_continuation(unit):
            groups[-1].append(unit)
        else:
            groups.append([unit])
            open_row = False
    return groups


def icon_slots(section: TargetSection) -> list[TargetSlot]:
    return sorted((s for s in section.slots if s.icon is not None and not is_callout_slot(s)), key=lambda s: s.display_order)

"""Cross-reference detection for v2 source units.

Finds phrases such as "see chapter 6.1.2.1", "(see Chapter 7, no. 1)",
"described in section 6.8", "refer to step 5", "Figure 2", "Appendix B",
"as described above" and document IDs such as "BI-VQD-10481-S", and resolves
each to a source section or unit where possible. Unresolved references stay in
the list, flagged, so the planner can report them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.schemas.v2 import CrossReference, RefKind, RefResolution, SourceDocument, SourceUnit, UnitType

_NUM = r"(\d+(?:\.\d+)*)"

# Order matters: earlier patterns claim their span first.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("chapter_item", re.compile(rf"\b(?:chapter|section)\s*{_NUM}\s*,\s*no\.?\s*(\d+)", re.I)),
    ("section", re.compile(rf"\b(?:chapter|section|sect\.|§)\s*{_NUM}(?![\d.]*\d)", re.I)),
    ("section", re.compile(rf"\b(?:described|defined|detailed|explained|outlined|specified)\s+in\s+(\d+\.\d+(?:\.\d+)*)\b", re.I)),
    ("step", re.compile(r"\b(?:see|refer\s+to|in|per|from)\s+step\s+(\d+)\b", re.I)),
    ("figure", re.compile(r"\b(?:figure|fig\.|image)\s+(\d+)\b", re.I)),
    ("table", re.compile(r"\b(?:see|refer\s+to|in|per)\s+table\s+(\d+)\b", re.I)),
    ("appendix", re.compile(r"\b(?:appendix|annex)\s+([A-Z0-9]+)\b", re.I)),
    ("relative", re.compile(r"\b(?:as\s+)?(?:described|mentioned|stated|defined|shown|outlined)\s+(above|below)\b", re.I)),
    ("relative", re.compile(r"\bsee\s+(above|below)\b", re.I)),
    ("relative", re.compile(r"\b(previous|preceding|following|next)\s+step\b", re.I)),
    ("external", re.compile(
        r"\b(BI-VQD-\d{4,6}(?:-[A-Za-z0-9]+)*|\d{3}-(?:BIS|BIP|OCS|ICS)-\d{5}(?:\s?-\s?(?:RD|AD)\d{2})?)\b"
    )),
]

_KIND = {
    "chapter_item": RefKind.SECTION,
    "section": RefKind.SECTION,
    "step": RefKind.STEP,
    "figure": RefKind.FIGURE,
    "table": RefKind.TABLE,
    "appendix": RefKind.APPENDIX,
    "relative": RefKind.RELATIVE,
    "external": RefKind.EXTERNAL_DOC,
}

# Units that list documents or define terms: their IDs are entries, not references.
_SKIP_TYPES = {UnitType.REFERENCE, UnitType.DEFINITION, UnitType.CAPTION, UnitType.FIGURE}


@dataclass
class _Match:
    pattern: str
    raw: str
    groups: tuple


def find_reference_phrases(text: str) -> list[_Match]:
    """All reference phrases in *text*, without overlaps, in reading order."""
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, _Match]] = []
    for name, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((m.start(), _Match(name, m.group(0).strip(), m.groups())))
    return [m for _, m in sorted(found, key=lambda x: x[0])]


def detect_cross_references(doc: SourceDocument) -> list[CrossReference]:
    sections_by_number = {s.number: s for s in doc.sections if s.number}
    refs: list[CrossReference] = []
    for section in doc.sections:
        for unit in section.units:
            if unit.is_boilerplate or unit.unit_type in _SKIP_TYPES:
                continue
            for match in find_reference_phrases(unit.text):
                target, resolution, direction = _resolve(match, unit, section, doc, sections_by_number)
                refs.append(CrossReference(
                    ref_id=f"XREF-{len(refs) + 1:03d}",
                    from_unit_id=unit.unit_id,
                    raw_text=match.raw,
                    ref_kind=_KIND[match.pattern],
                    target=target,
                    resolution=resolution,
                    direction=direction,
                ))
    return refs


def _resolve(match: _Match, unit: SourceUnit, section, doc: SourceDocument, sections_by_number):
    kind = match.pattern
    if kind == "external":
        return re.sub(r"\s+", "", match.groups[0]), RefResolution.EXTERNAL, None
    if kind == "relative":
        word = match.groups[0].lower()
        direction = "before" if word in ("above", "previous", "preceding") else "after"
        return None, RefResolution.UNRESOLVED, direction
    if kind in ("section", "chapter_item"):
        target_section = sections_by_number.get(match.groups[0].rstrip("."))
        if target_section is None:
            return None, RefResolution.UNRESOLVED, None
        if kind == "chapter_item":
            row = _numbered_row(target_section, match.groups[1])
            if row is not None:
                return row.unit_id, RefResolution.RESOLVED, None
        return target_section.section_id, RefResolution.RESOLVED, None
    if kind == "step":
        step = next((u for u in section.units
                     if u.unit_type == UnitType.PROCEDURE_STEP and (u.list_number or "").rstrip(".)") == match.groups[0]),
                    None)
        return (step.unit_id, RefResolution.RESOLVED, None) if step else (None, RefResolution.UNRESOLVED, None)
    if kind in ("figure", "table"):
        label = re.compile(rf"^(?:figure|fig\.|image|table)\s*{match.groups[0]}\b", re.I)
        for u in doc.iter_units():
            if u.unit_type in (UnitType.CAPTION, UnitType.FIGURE) and label.match(u.text):
                target = next((r.target_unit_id for r in u.relations if r.type.value == "caption_of"), u.unit_id)
                return target, RefResolution.RESOLVED, None
        return None, RefResolution.UNRESOLVED, None
    if kind == "appendix":
        letter = match.groups[0].lower()
        for s in doc.sections:
            if re.match(rf"^(?:appendix|annex)\s+{re.escape(letter)}\b", s.heading, re.I):
                return s.section_id, RefResolution.RESOLVED, None
        return None, RefResolution.UNRESOLVED, None
    return None, RefResolution.UNRESOLVED, None


def _numbered_row(section, item: str) -> Optional[SourceUnit]:
    """The row numbered *item* ("No." column) in a reference-list section."""
    for u in section.units:
        if u.table_ref and u.table_ref.cells:
            first = u.table_ref.cells[0].text.strip().rstrip(".")
            if first == item:
                return u
    return None

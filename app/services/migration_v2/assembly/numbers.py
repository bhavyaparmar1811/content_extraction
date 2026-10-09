"""The whole-document numbering (Phase 12): which chapters are present, and the number of every heading and
referenced passage, as Word will show them.

Numbers follow the renderer exactly:
- chapters are the template's headings, numbered by Word over the chapters that remain. An optional chapter with no
  content is removed (``present_sections``), so the ones after it move up ("10 DOCUMENT HISTORY" becomes 9);
- a source sub-heading kept in a chapter is a ``Heading n`` paragraph on the template's heading numbering, one level
  below the chapter per ``list_level`` ("6.1", "6.1.2"); a skipped level counts from 1, as in Word;
- a passage has the number of the heading or chapter that holds it, plus its own: the "No." cell of a list row
  ("Chapter 7, no. 1"), or its place in a numbered list (step 4).

A source section that has no heading of its own any more (merged into a larger chapter, or one of a table-only
chapter's parts) gets its holder's number, marked ``exact: false``. A reference target with no claim anywhere is
left out: the resolver reports it.

The renderer bookmarks each chapter and heading (``bookmark``); references become Word REF fields to them, and the
post-render check compares Word's numbering of each bookmarked paragraph with this map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.schemas.v2 import (
    Claim,
    ClaimKind,
    NumberKind,
    NumberMap,
    NumberMapEntry,
    SectionDraft,
    SectionPlan,
    SourceDocument,
    TemplateModel,
    find_ref_tokens,
)

_NO_HEADER = re.compile(r"^\s*(?:no\.?|nr\.?|#|number|item)\s*$", re.I)
_NO_LIST = object()


def bookmark_name(source_id: str) -> str:
    """A hidden Word bookmark for a numbered target (letters, digits and '_', at most 40 characters)."""
    return ("_Ref_" + re.sub(r"[^A-Za-z0-9]", "_", source_id))[:40]


def present_sections(template: TemplateModel, drafts: Iterable[SectionDraft], accepted_gaps: Iterable[str] = ()) -> list[str]:
    """Template sections in the rendered document, in order: an optional chapter with nothing in it is removed."""
    accepted = set(accepted_gaps)
    by_section = {d.target_section_id: d for d in drafts}
    out = []
    for section in sorted(template.sections, key=lambda s: s.display_order):
        draft = by_section.get(section.section_id)
        has = draft is not None and any(
            c for s in draft.slots for c in s.claims if not (c.is_gap_marker and s.slot_id in accepted))
        if has or not section.optional_marker:
            out.append(section.section_id)
    return out


@dataclass
class Placed:
    """A claim with its place in the document and the heading or chapter that holds it."""

    claim: Claim
    section_id: str
    slot_id: str
    position: int
    holder: NumberMapEntry
    step: Optional[int] = None


def _section_numbers(template: TemplateModel, present: list[str]) -> dict[str, str]:
    levels = {s.section_id: max(1, s.level) for s in template.sections}
    counters: dict[int, int] = {}
    out = {}
    for sid in present:
        level = levels[sid]
        counters[level] = counters.get(level, 0) + 1
        for deeper in [k for k in counters if k > level]:
            del counters[deeper]
        out[sid] = ".".join(str(counters.setdefault(k, 1)) for k in range(1, level + 1))
    return out


def _item_number(claim: Claim, source: SourceDocument, units: dict) -> Optional[str]:
    """A list row's 'No.' cell, as drafted."""
    unit = units.get(claim.source_unit_ids[0]) if claim.source_unit_ids else None
    if claim.kind != ClaimKind.TABLE_ROW or unit is None or unit.table_ref is None or not unit.table_ref.header_cells:
        return None
    if not _NO_HEADER.match(unit.table_ref.header_cells[0] or ""):
        return None
    first = (claim.text or "").split("|")[0].strip().rstrip(".")
    return first or None


def place_claims(template: TemplateModel, drafts: Iterable[SectionDraft], present: list[str],
                 numbers: dict[str, str]) -> tuple[list[Placed], list[NumberMapEntry]]:
    """Every claim of the present chapters in document order, with its holder; and the chapter and heading entries."""
    by_section = {d.target_section_id: d for d in drafts}
    placed: list[Placed] = []
    entries: list[NumberMapEntry] = []
    position = 0
    for sid in present:
        chapter = NumberMapEntry(source_id=sid, target_section_id=sid, target_number=numbers[sid],
                                 bookmark=bookmark_name(sid), kind=NumberKind.SECTION)
        entries.append(chapter)
        draft = by_section.get(sid)
        if draft is None:
            continue
        holder, counters = chapter, {}
        for slot in draft.slots:
            run, steps = _NO_LIST, {}  # a list: consecutive bullets and steps of one callout kind (render.list_blocks)
            for claim in slot.claims:
                step = None
                is_list = claim.kind in (ClaimKind.BULLET, ClaimKind.STEP) and not claim.is_gap_marker
                if not is_list:
                    run, steps = _NO_LIST, {}
                if claim.kind == ClaimKind.HEADING:
                    depth = 1 + claim.list_level
                    counters[depth] = counters.get(depth, 0) + 1
                    for deeper in [k for k in counters if k > depth]:
                        del counters[deeper]
                    number = ".".join([numbers[sid]] + [str(counters.setdefault(k, 1)) for k in range(1, depth + 1)])
                    holder = NumberMapEntry(source_id=claim.source_section_id, target_section_id=sid, target_number=number,
                                            bookmark=bookmark_name(claim.source_section_id), kind=NumberKind.HEADING,
                                            claim_id=claim.claim_id)
                    entries.append(holder)
                elif is_list:
                    if run is _NO_LIST or run != claim.callout_kind:
                        steps = {}
                    run = claim.callout_kind
                    if claim.kind == ClaimKind.STEP:  # one numbered list per run, levels counted like Word
                        steps[claim.list_level] = steps.get(claim.list_level, 0) + 1
                        for deeper in [k for k in steps if k > claim.list_level]:
                            del steps[deeper]
                        step = steps[claim.list_level]
                placed.append(Placed(claim, sid, slot.slot_id, position, holder, step))
                position += 1
    return placed, entries


def build_number_map(template: TemplateModel, drafts: list[SectionDraft], source: SourceDocument,
                     section_plan: Optional[SectionPlan] = None, accepted_gaps: Iterable[str] = (),
                     job_id: Optional[str] = None, version: Optional[int] = None) -> NumberMap:
    present = present_sections(template, drafts, accepted_gaps)
    numbers = _section_numbers(template, present)
    placed, entries = place_claims(template, drafts, present, numbers)
    known = {e.source_id for e in entries}
    units = {u.unit_id: u for u in source.iter_units()}
    source_sections = {s.section_id: s for s in source.sections}
    targets = list(dict.fromkeys(t for p in placed for t in find_ref_tokens(p.claim.text)))

    for target in targets:
        if target in known:
            continue
        entry = None
        if target in units:
            entry = _unit_entry(target, placed, units, source)
        elif target in source_sections:
            entry = _section_entry(target, placed, entries, source, section_plan, present)
        if entry is not None:
            entries.append(entry)
            known.add(target)
    removed = [s.section_id for s in sorted(template.sections, key=lambda s: s.display_order) if s.section_id not in present]
    return NumberMap(job_id=job_id, version=version, entries=entries, sections_removed=removed)


def _unit_entry(unit_id: str, placed: list[Placed], units: dict, source: SourceDocument) -> Optional[NumberMapEntry]:
    holding = next((p for p in placed if unit_id in p.claim.source_unit_ids and p.claim.kind != ClaimKind.HEADING
                    and not p.claim.is_gap_marker), None)
    if holding is None:
        return None
    item = _item_number(holding.claim, source, units) or (str(holding.step) if holding.step else None)
    return NumberMapEntry(source_id=unit_id, target_section_id=holding.section_id,
                          target_number=holding.holder.target_number, bookmark=holding.holder.bookmark,
                          kind=NumberKind.UNIT, claim_id=holding.claim.claim_id, item_number=item)


def _subtree(section_id: str, source: SourceDocument) -> set[str]:
    out, grew = {section_id}, True
    while grew:
        grew = False
        for s in source.sections:
            if s.parent_id in out and s.section_id not in out:
                out.add(s.section_id)
                grew = True
    return out


def _section_entry(section_id: str, placed: list[Placed], entries: list[NumberMapEntry], source: SourceDocument,
                   section_plan: Optional[SectionPlan], present: list[str]) -> Optional[NumberMapEntry]:
    by_id = {s.section_id: s for s in source.sections}
    # A source chapter that leads a template chapter's mapping is that chapter.
    for m in (section_plan.mappings if section_plan else []):
        if (m.source_section_ids and m.source_section_ids[0] == section_id and by_id[section_id].parent_id is None
                and m.target_section_id in present):
            chapter = next(e for e in entries if e.source_id == m.target_section_id)
            return chapter.model_copy(update={"source_id": section_id})
    # Otherwise: where its passages are now. No heading of its own, so the number is its holder's.
    tree = _subtree(section_id, source)
    unit_ids = {u.unit_id for s in source.sections if s.section_id in tree for u in s.units}
    holding = [p for p in placed if unit_ids & set(p.claim.source_unit_ids) and not p.claim.is_gap_marker]
    if not holding:
        return None
    first = holding[0]
    chapters = list(dict.fromkeys(p.section_id for p in holding))
    split = f"; its passages are split over {', '.join(chapters)}" if len(chapters) > 1 else ""
    return NumberMapEntry(source_id=section_id, target_section_id=first.section_id,
                          target_number=first.holder.target_number, bookmark=first.holder.bookmark,
                          kind=first.holder.kind, claim_id=first.holder.claim_id, exact=False,
                          note=f"{by_id[section_id].number or section_id} has no heading of its own in the new document; "
                               f"it is now part of {first.holder.target_number}{split}")


def claim_holders(template: TemplateModel, drafts: list[SectionDraft],
                  accepted_gaps: Iterable[str] = ()) -> dict[str, NumberMapEntry]:
    """claim_id → the chapter or heading that holds it, numbered as in the document."""
    present = present_sections(template, drafts, accepted_gaps)
    placed, _ = place_claims(template, drafts, present, _section_numbers(template, present))
    return {p.claim.claim_id: p.holder for p in placed}

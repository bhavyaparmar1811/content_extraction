"""Cross-reference resolution (Phase 12): each ``{{ref:...}}`` token → what the new document shows.

The source's own phrase is kept and only its numbers change: "see chapter 6.1.2.1" becomes "see chapter 6.1.2.1" or
"see chapter 6.2.1", "Chapter 8, no. 17" keeps its entry number and takes the new chapter number. The chapter or
heading number is rendered as a Word REF field to the target's bookmark (``\\w``: full number), so it stays right
when the document is edited; its cached value is the number from the ``NumberMap``.

- a target with its own number → ``resolved``;
- a target merged into a larger part, or split → ``merged``: the reference points to what holds it now (the first
  holder, for a split), with a medium issue to check the wording;
- a target omitted or not drafted → ``unresolved``: the phrase stays as written and a high
  ``broken_cross_reference`` issue blocks the job until a reviewer fixes it;
- references to other documents are not tokens: they stay verbatim (protected values);
- relative phrases ("as described above", "see below", "the next step") have no target; the passage just before
  (above) or after (below) in the source must still be on that side in the new document, else a medium issue.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from app.schemas.v2 import (
    ClaimKind,
    Gate,
    IssueCategory,
    IssueSource,
    NumberMap,
    RefKind,
    RefStatus,
    ResolvedRef,
    SectionDraft,
    Severity,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
    find_ref_tokens,
)

from ..drafting.refs import raw_text_by_target
from .numbers import place_claims, present_sections, _section_numbers


def _issue(key: str, severity: Severity, message: str, gate: Optional[Gate] = None, target: Optional[str] = None,
           unit_ids: Optional[list[str]] = None, claim_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"xref|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(issue_id=f"ISS-{digest}", severity=severity, category=IssueCategory.CROSS_REFERENCE,
                           source=IssueSource.DETERMINISTIC, gate=gate, target_section_id=target,
                           unit_ids=unit_ids or [], claim_ids=claim_ids or [], message=message)


def _number_pattern(number: str) -> re.Pattern:
    return re.compile(rf"(?<![\d.]){re.escape(number)}(?!\d|\.\d)")


def show(phrase: str, old_number: Optional[str], new_number: Optional[str], old_item: Optional[str] = None,
         new_item: Optional[str] = None) -> tuple[str, Optional[int]]:
    """The phrase with its numbers replaced; and where the new chapter/heading number starts (None: not in it)."""
    text, at = phrase, None
    if old_number and new_number:
        m = _number_pattern(old_number).search(text)
        if m:
            text, at = text[:m.start()] + new_number + text[m.end():], m.start()
    if old_item and new_item and old_item != new_item:
        start = at + len(new_number) if at is not None else 0
        m = re.compile(rf"(\b(?:no\.?|step)\s*){re.escape(old_item)}\b", re.I).search(text, start)
        if m:
            text = text[:m.start()] + m.group(1) + new_item + text[m.end():]
    return text, at


def _old_numbers(target: str, phrase: str, source: SourceDocument) -> tuple[Optional[str], Optional[str]]:
    """The source chapter/section number the phrase uses for *target*, and its entry or step number."""
    sections = {s.section_id: s for s in source.sections}
    if target in sections:
        return sections[target].number, None
    unit = next((u for u in source.iter_units() if u.unit_id == target), None)
    if unit is None:
        return None, None
    item = re.search(r"\b(?:no\.?|step)\s*(\d+)\b", phrase, re.I)
    # "Chapter 7, no. 1" names the list's section (or a section above it); "step 4" names none.
    number, node = None, sections.get(unit.section_id)
    while node is not None and number is None:
        if node.number and _number_pattern(node.number).search(phrase):
            number = node.number
        node = sections.get(node.parent_id) if node.parent_id else None
    return number, (item.group(1) if item else None)


def resolve_refs(drafts: list[SectionDraft], number_map: NumberMap, source: SourceDocument
                 ) -> tuple[list[ResolvedRef], list[ValidationIssue]]:
    phrases = raw_text_by_target(source)
    refs: list[ResolvedRef] = []
    issues: list[ValidationIssue] = []
    for draft in drafts:
        for claim in draft.iter_claims():
            for target in dict.fromkeys(find_ref_tokens(claim.text)):
                phrase = next((phrases[(u, target)] for u in claim.source_unit_ids if (u, target) in phrases), target)
                entry = number_map.lookup(target)
                if entry is None:
                    refs.append(ResolvedRef(claim_id=claim.claim_id, target=target, source_phrase=phrase, text=phrase,
                                            status=RefStatus.UNRESOLVED, note="the target is not in the new document"))
                    issues.append(_issue(
                        f"unresolved|{claim.claim_id}|{target}", Severity.HIGH,
                        f"{claim.claim_id}: the reference '{phrase}' points to {target}, which is not in the new document "
                        "(omitted or not drafted). Fix the reference, or resolve this issue to keep it as written.",
                        Gate.BROKEN_CROSS_REFERENCE, draft.target_section_id, list(claim.source_unit_ids), [claim.claim_id]))
                    continue
                old_number, old_item = _old_numbers(target, phrase, source)
                text, at = show(phrase, old_number, entry.target_number, old_item, entry.item_number)
                status = RefStatus.RESOLVED if entry.exact else RefStatus.MERGED
                refs.append(ResolvedRef(
                    claim_id=claim.claim_id, target=target, source_phrase=phrase, text=text,
                    number=entry.target_number if at is not None else None, number_at=at,
                    bookmark=entry.bookmark if at is not None else None, status=status, note=entry.note))
                if status == RefStatus.MERGED:
                    issues.append(_issue(
                        f"merged|{claim.claim_id}|{target}", Severity.MEDIUM,
                        f"{claim.claim_id}: '{phrase}' now reads '{text}': {entry.note}. Check that the reference still "
                        "says what the source meant.", None, draft.target_section_id, list(claim.source_unit_ids),
                        [claim.claim_id]))
    return refs, issues


def relative_issues(drafts: list[SectionDraft], template: TemplateModel, source: SourceDocument) -> list[ValidationIssue]:
    """'above' / 'below' still true: the source passage just before (or after) the referring one keeps its side."""
    present = present_sections(template, drafts)
    placed, _ = place_claims(template, drafts, present, _section_numbers(template, present))
    position: dict[str, int] = {}
    claim_of: dict[str, str] = {}
    for p in placed:
        if p.claim.kind == ClaimKind.HEADING or p.claim.is_gap_marker:
            continue
        for u in p.claim.source_unit_ids:
            position.setdefault(u, p.position)
            claim_of.setdefault(u, p.claim.claim_id)
    units = {u.unit_id: u for u in source.iter_units()}
    issues = []
    for ref in source.cross_references:
        if ref.ref_kind != RefKind.RELATIVE or ref.direction not in ("before", "after") or ref.from_unit_id not in position:
            continue
        unit = units.get(ref.from_unit_id)
        if unit is None:
            continue
        section = next(s for s in source.sections if s.section_id == unit.section_id)
        siblings = [u for u in section.units if u.unit_id in position]
        earlier = [u for u in siblings if u.seq < unit.seq]
        later = [u for u in siblings if u.seq > unit.seq]
        neighbour = earlier[-1] if ref.direction == "before" and earlier else later[0] if ref.direction == "after" and later else None
        if neighbour is None:
            continue
        here, there = position[ref.from_unit_id], position[neighbour.unit_id]
        moved = there > here if ref.direction == "before" else there < here
        if moved:
            side = "above" if ref.direction == "before" else "below"
            issues.append(_issue(
                f"relative|{ref.ref_id}", Severity.MEDIUM,
                f"{claim_of[ref.from_unit_id]}: '{ref.raw_text}' refers to what is {side} it, but {neighbour.unit_id} "
                f"(just {side} it in the source) is now on the other side. Check the reference.",
                unit_ids=[ref.from_unit_id, neighbour.unit_id], claim_ids=[claim_of[ref.from_unit_id]]))
    return issues

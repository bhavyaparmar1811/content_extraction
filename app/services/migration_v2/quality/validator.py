"""Deterministic validator of a job's drafts (Phase 10, migration_plan.md §17.1).

Builds on the Phase 9 draft checks (``drafting/checks.py``: traceability, coverage, reference tokens, gap
markers and the Phase 6 preservation comparators) and adds:

- structure: every drafted slot belongs to its template section and has an anchor;
- order: claims follow the source sequence inside a slot (a procedure out of order is a gate);
- formatting: no placeholder such as ``[TBD]`` left unnoticed;
- callouts: a claim's ``callout_kind`` is its slot's kind or the kind assigned to its passages, and is in the palette;
- GWP style (soft): the job's deterministic STY rules on the slots the slot plan rewrites (``style.py``).

Issue IDs are stable hashes of what was found, so a reviewer's resolution carries over to the next run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.v2 import (
    ClaimKind,
    ContentType,
    Gate,
    GwpRuleSet,
    IssueCategory,
    IssueSource,
    MigrationAction,
    ProtectedFacts,
    SectionDraft,
    Severity,
    SlotPlan,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)

from ..drafting.checks import draft_issues
from ..drafting.drafter import BELOW_OFFSET, below_units
from .style import CheckedClaim, check_style, prose, style_rules

CALLOUT_TAG = "[callout]"  # callout kinds come from the slot plan: a rebuild of the section fixes them
REWRITE_ACTIONS = (MigrationAction.EXTRACT_AND_REWRITE, MigrationAction.EXTRACT_AND_CONSOLIDATE,
                   MigrationAction.REWRITE_AS_ORDERED_PROCEDURE)
_PLACEHOLDER = re.compile(r"\[(?:TBD|TBC|TODO|insert[^\]]{0,40}|placeholder[^\]]{0,40})\]|\bTBD\b|\bTBC\b|\bX{3,}\b"
                          r"|<(?:insert|enter|add|name|date)[^<>]{0,40}>", re.I)


@dataclass
class Validation:
    issues: list[ValidationIssue] = field(default_factory=list)
    unit_coverage: float = 1.0
    soft_scores: dict[str, float] = field(default_factory=dict)


def _issue(key: str, severity: Severity, category: IssueCategory, message: str, gate: Optional[Gate] = None,
           target: Optional[str] = None, slot: Optional[str] = None, unit_ids: Optional[list[str]] = None,
           claim_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"validator|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(issue_id=f"ISS-{digest}", severity=severity, category=category, source=IssueSource.DETERMINISTIC,
                           gate=gate, target_section_id=target, slot_id=slot, unit_ids=unit_ids or [],
                           claim_ids=claim_ids or [], message=message)


def structure_issues(drafts: list[SectionDraft], template: TemplateModel) -> list[ValidationIssue]:
    sections = {s.section_id: s for s in template.sections}
    issues = []
    for draft in drafts:
        target = sections.get(draft.target_section_id)
        slots = {s.slot_id: s for s in target.slots} if target else {}
        for slot in draft.slots:
            units = list(dict.fromkeys(u for c in slot.claims for u in c.source_unit_ids))
            if slot.slot_id not in slots:
                issues.append(_issue(f"slot|{draft.target_section_id}|{slot.slot_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                     f"Slot {slot.slot_id} is not a slot of template section {draft.target_section_id}; "
                                     "its content cannot be placed.", Gate.UNACCOUNTED_SOURCE, draft.target_section_id,
                                     slot.slot_id, units))
                continue
            if slots[slot.slot_id].anchor is None and slot.claims:
                issues.append(_issue(f"anchor|{slot.slot_id}", Severity.MEDIUM, IssueCategory.STRUCTURE,
                                     f"Slot {slot.slot_id} has no anchor in the template; the renderer cannot place it.",
                                     target=draft.target_section_id, slot=slot.slot_id))
    return issues


def order_issues(drafts: list[SectionDraft], source: SourceDocument, template: TemplateModel,
                 slot_plan: Optional[SlotPlan] = None) -> list[ValidationIssue]:
    """Claims out of source order inside a slot; passages shown below a section's tables belong after its rows."""
    seq = {u.unit_id: u.seq for u in source.iter_units()}
    below = below_units(slot_plan)
    slots = {s.slot_id: s for s in template.iter_slots()}
    issues = []
    for draft in drafts:
        for slot in draft.slots:
            claims = [c for c in slot.claims if not c.is_gap_marker and c.kind != ClaimKind.HEADING
                      and any(u in seq for u in c.source_unit_ids)]
            furthest, out_of_order = None, []  # (position, claim) of the latest source passage seen so far
            for claim in claims:
                position = min(seq[u] + (BELOW_OFFSET if (slot.slot_id, u) in below else 0)
                               for u in claim.source_unit_ids if u in seq)
                if furthest is not None and position < furthest[0]:
                    out_of_order.append((claim, furthest[1]))
                else:
                    furthest = (position, claim)
            if not out_of_order:
                continue
            tslot = slots.get(slot.slot_id)
            procedure = (tslot is not None and tslot.content_type == ContentType.ORDERED_PROCEDURE) or any(
                c.kind == ClaimKind.STEP for c, _ in out_of_order)
            ids = [c.claim_id for c, _ in out_of_order]
            first, before = out_of_order[0]
            issues.append(_issue(
                f"order|{slot.slot_id}|{','.join(ids)}", Severity.HIGH, IssueCategory.ORDER,
                f"Slot {slot.slot_id}: {len(ids)} claim(s) are out of source order, e.g. {first.claim_id} "
                f"({', '.join(first.source_unit_ids)}) comes after {before.claim_id} ({', '.join(before.source_unit_ids)}).",
                Gate.SEQUENCE_VIOLATION if procedure else None, draft.target_section_id, slot.slot_id,
                list(dict.fromkeys(u for c, _ in out_of_order for u in c.source_unit_ids)), ids))
    return issues


def callout_issues(drafts: list[SectionDraft], slot_plan: SlotPlan, template: TemplateModel) -> list[ValidationIssue]:
    slots = {s.slot_id: s for s in template.iter_slots()}
    palette = {c.kind for c in template.callout_palette}
    issues = []
    for draft in drafts:
        plan = slot_plan.section(draft.target_section_id)
        assigned: dict[str, set] = {}
        for a in (plan.callout_assignments if plan else []):
            for u in a.unit_ids:
                assigned.setdefault(u, set()).add(a.kind)
        for slot in draft.slots:
            tslot = slots.get(slot.slot_id)
            for claim in slot.claims:
                if claim.callout_kind is None or claim.is_gap_marker:
                    continue
                kind = claim.callout_kind
                fits = (tslot is not None and tslot.callout_kind == kind) or (
                    claim.source_unit_ids and all(kind in assigned.get(u, set()) for u in claim.source_unit_ids))
                if kind not in palette:
                    why = f"the template palette has no '{kind.value}' callout"
                elif not fits:
                    why = f"neither its slot nor the slot plan's callout assignments give it '{kind.value}'"
                else:
                    continue
                issues.append(_issue(f"callout|{claim.claim_id}|{kind.value}", Severity.HIGH, IssueCategory.FORMATTING,
                                     f"{CALLOUT_TAG} {claim.claim_id} is in a '{kind.value}' callout, but {why}.",
                                     target=draft.target_section_id, slot=slot.slot_id,
                                     unit_ids=list(claim.source_unit_ids), claim_ids=[claim.claim_id]))
    return issues


def placeholder_issues(drafts: list[SectionDraft], source: SourceDocument) -> list[ValidationIssue]:
    texts = {u.unit_id: u.text or "" for u in source.iter_units()}
    issues = []
    for draft in drafts:
        for slot in draft.slots:
            for claim in slot.claims:
                if claim.is_gap_marker:
                    continue
                found = sorted({m.group(0) for m in _PLACEHOLDER.finditer(claim.text)})
                if not found:
                    continue
                in_source = all(any(f.lower() in texts.get(u, "").lower() for u in claim.source_unit_ids) for f in found)
                issues.append(_issue(
                    f"placeholder|{claim.claim_id}|{','.join(found)}", Severity.MEDIUM if in_source else Severity.HIGH,
                    IssueCategory.FORMATTING,
                    f"{claim.claim_id} contains the placeholder {', '.join(repr(f) for f in found)}"
                    + (" (as in the source; the author must fill it)." if in_source else ", which its source does not have."),
                    target=draft.target_section_id, slot=slot.slot_id, unit_ids=list(claim.source_unit_ids),
                    claim_ids=[claim.claim_id]))
    return issues


def style_items(drafts: list[SectionDraft], slot_plan: SlotPlan, source: SourceDocument,
                rules: Optional[GwpRuleSet]) -> list[CheckedClaim]:
    checkable = {r.rule_id: r for r in style_rules(rules.rules if rules else [])}
    if not checkable:
        return []
    mappings = {m.slot_id: m for s in slot_plan.sections for m in s.slot_mappings}
    texts = {u.unit_id: u.text or "" for u in source.iter_units()}
    items = []
    for draft in drafts:
        for slot in draft.slots:
            m = mappings.get(slot.slot_id)
            if m is None or m.migration_action not in REWRITE_ACTIONS:
                continue
            slot_rules = [checkable[r] for r in dict.fromkeys(m.rule_ids) if r in checkable]
            if not slot_rules:
                continue
            for claim in slot.claims:
                if prose(claim):
                    items.append(CheckedClaim(claim, draft.target_section_id, slot.slot_id, slot_rules,
                                              " ".join(texts.get(u, "") for u in claim.source_unit_ids)))
    return items


def validate_drafts(drafts: Iterable[SectionDraft], slot_plan: SlotPlan, source: SourceDocument, template: TemplateModel,
                    facts: ProtectedFacts, rules: Optional[GwpRuleSet] = None) -> Validation:
    drafts = list(drafts)
    issues, coverage = draft_issues(drafts, slot_plan, source, template, facts)
    issues += structure_issues(drafts, template)
    issues += order_issues(drafts, source, template, slot_plan)
    issues += callout_issues(drafts, slot_plan, template)
    issues += placeholder_issues(drafts, source)
    style = check_style(style_items(drafts, slot_plan, source, rules))
    issues += style.issues
    unique = list({i.issue_id: i for i in issues}.values())
    return Validation(unique, coverage, {"unit_coverage": coverage, **style.scores})

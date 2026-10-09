"""Deterministic checks of a job's drafts against its slot plan (Phase 9; Phase 10 adds the critic and repair).

- every passage the slot plan puts in a slot is cited by a claim of that slot   → ``unaccounted_source``;
- a claim cites only passages planned for its slot (headings: a source section
  whose passages are in the slot)                                               → ``unsupported_claim``;
- every internal reference of a cited passage survives as a ``{{ref:...}}``
  token, and no claim carries a token its passages do not have                 → ``broken_cross_reference``;
- every ``source_content_not_found`` slot has a gap marker, and gap markers
  appear only there                                                             → ``missing_slot`` / ``unsupported_claim``;
- the Phase 6 preservation comparators (values, references, modality, roles),
  run on the drafts with reference tokens turned back into the source wording;
- the drafter's unresolved items are listed (no gate).
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

from app.schemas.v2 import (
    ClaimKind,
    Gate,
    IssueCategory,
    IssueSource,
    MappingStatus,
    ProtectedFacts,
    QualityReport,
    SectionDraft,
    Severity,
    SlotPlan,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)

from ..planning.slot_signals import is_callout_slot
from ..quality.preservation import check_preservation
from .refs import detokenized, internal_refs, tokenize, tokens_of


def _issue(key: str, severity: Severity, category: IssueCategory, message: str, gate: Optional[Gate] = None,
           target: Optional[str] = None, slot: Optional[str] = None, unit_ids: Optional[list[str]] = None,
           claim_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"drafts|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(issue_id=f"ISS-{digest}", severity=severity, category=category, source=IssueSource.DETERMINISTIC,
                           gate=gate, target_section_id=target, slot_id=slot, unit_ids=unit_ids or [],
                           claim_ids=claim_ids or [], message=message)


def planned_units(slot_plan: SlotPlan, template: TemplateModel) -> dict[str, list[str]]:
    """slot_id → the passages a draft of that slot must cite (promoted passages count once, in their content slot)."""
    out: dict[str, list[str]] = {}
    slots = {s.slot_id: s for s in template.iter_slots()}
    for section in slot_plan.sections:
        content = {u for m in section.slot_mappings if m.slot_id in slots and not is_callout_slot(slots[m.slot_id])
                   for u in m.source_unit_ids}
        for m in section.slot_mappings:
            slot = slots.get(m.slot_id)
            if slot is None:
                continue
            ids = [u for u in m.source_unit_ids if not (is_callout_slot(slot) and u in content)]
            out[m.slot_id] = ids
    return out


def _slot_part(claims, unit_id: str, text: str) -> str:
    """The part of a passage a slot drafts: its claims' spans when the passage was split between slots, else all of it."""
    spans = list(dict.fromkeys((s.start, s.end) for c in claims if unit_id in c.source_unit_ids
                               for s in c.spans if s.unit_id == unit_id))
    if not spans:
        return text
    full = re.sub(r"\s+", " ", text).strip()
    return " ".join(full[a:b] for a, b in sorted(spans))


def draft_issues(drafts: Iterable[SectionDraft], slot_plan: SlotPlan, source: SourceDocument, template: TemplateModel,
                 facts: ProtectedFacts) -> tuple[list[ValidationIssue], float]:
    drafts = list(drafts)
    planned = planned_units(slot_plan, template)
    status = {m.slot_id: m.status for s in slot_plan.sections for m in s.slot_mappings}
    unit_by_id = {u.unit_id: u for u in source.iter_units()}
    refs = internal_refs(source)
    unit_section = {u.unit_id: u.section_id for u in source.iter_units()}
    parents = {s.section_id: s.parent_id for s in source.sections}
    issues: list[ValidationIssue] = []
    drafted_slots: set[str] = set()
    covered = 0

    def ancestors(section_id: str) -> set[str]:
        out = set()
        while section_id:
            out.add(section_id)
            section_id = parents.get(section_id)
        return out

    for draft in drafts:
        for slot in draft.slots:
            drafted_slots.add(slot.slot_id)
            want = planned.get(slot.slot_id, [])
            allowed_sections = {a for u in want for a in ancestors(unit_section.get(u, ""))}
            cited: set[str] = set()
            for claim in slot.claims:
                if claim.is_gap_marker:
                    if status.get(slot.slot_id) != MappingStatus.SOURCE_CONTENT_NOT_FOUND:
                        issues.append(_issue(f"gap|{claim.claim_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                             f"{claim.claim_id} is a gap marker in slot {slot.slot_id}, which has source content.",
                                             Gate.UNSUPPORTED_CLAIM, draft.target_section_id, slot.slot_id, claim_ids=[claim.claim_id]))
                    continue
                if claim.kind == ClaimKind.HEADING:
                    if claim.source_section_id not in allowed_sections:
                        issues.append(_issue(f"heading|{claim.claim_id}", Severity.HIGH, IssueCategory.TRACEABILITY,
                                             f"Heading {claim.claim_id} comes from {claim.source_section_id}, which has no passage "
                                             f"in slot {slot.slot_id}.", Gate.UNSUPPORTED_CLAIM, draft.target_section_id,
                                             slot.slot_id, claim_ids=[claim.claim_id]))
                    continue
                stray = [u for u in claim.source_unit_ids if u not in want]
                if stray:
                    issues.append(_issue(f"stray|{claim.claim_id}", Severity.HIGH, IssueCategory.TRACEABILITY,
                                         f"{claim.claim_id} cites {stray}, which the slot plan does not put in slot {slot.slot_id}.",
                                         Gate.UNSUPPORTED_CLAIM, draft.target_section_id, slot.slot_id, stray, [claim.claim_id]))
                cited.update(claim.source_unit_ids)
                own = {t for u in claim.source_unit_ids if u in unit_by_id
                       for t in tokens_of(tokenize(unit_by_id[u], refs.get(u, [])))}
                foreign = [t for t in tokens_of(claim.text) if t not in own]
                if foreign:
                    issues.append(_issue(f"foreign|{claim.claim_id}", Severity.HIGH, IssueCategory.CROSS_REFERENCE,
                                         f"{claim.claim_id} carries reference tokens {foreign} its passages do not have.",
                                         Gate.BROKEN_CROSS_REFERENCE, draft.target_section_id, slot.slot_id,
                                         claim_ids=[claim.claim_id]))
            missing = [u for u in want if u not in cited]
            covered += len(want) - len(missing)
            if missing:
                issues.append(_issue(f"uncited|{slot.slot_id}", Severity.CRITICAL, IssueCategory.TRACEABILITY,
                                     f"Slot {slot.slot_id}: {len(missing)} planned passage(s) are not cited: {', '.join(missing[:8])}",
                                     Gate.UNACCOUNTED_SOURCE, draft.target_section_id, slot.slot_id, missing))
            for unit_id in want:
                part = _slot_part(slot.claims, unit_id, tokenize(unit_by_id[unit_id], refs.get(unit_id, []))) \
                    if unit_id in unit_by_id else ""
                expected = tokens_of(part)
                kept = {t for c in slot.claims if unit_id in c.source_unit_ids for t in tokens_of(c.text)}
                lost = [t for t in expected if t not in kept]
                if lost and unit_id in cited:
                    issues.append(_issue(f"lost|{slot.slot_id}|{unit_id}", Severity.HIGH, IssueCategory.CROSS_REFERENCE,
                                         f"Slot {slot.slot_id}: the reference(s) {lost} of {unit_id} were lost.",
                                         Gate.BROKEN_CROSS_REFERENCE, draft.target_section_id, slot.slot_id, [unit_id]))
        for item in draft.unresolved_items:
            issues.append(_issue(f"unresolved|{draft.target_section_id}|{item}", Severity.MEDIUM, IssueCategory.STRUCTURE,
                                 f"{draft.target_section_id}: {item}", target=draft.target_section_id))

    for slot_id, st in status.items():
        if st == MappingStatus.SOURCE_CONTENT_NOT_FOUND and slot_id not in drafted_slots:
            issues.append(_issue(f"nogap|{slot_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                 f"Slot {slot_id} has no source content but its draft has no gap marker.", Gate.MISSING_SLOT,
                                 slot=slot_id))
        elif planned.get(slot_id) and slot_id not in drafted_slots:
            issues.append(_issue(f"nodraft|{slot_id}", Severity.CRITICAL, IssueCategory.TRACEABILITY,
                                 f"Slot {slot_id} has planned passages but no draft.", Gate.UNACCOUNTED_SOURCE, slot=slot_id,
                                 unit_ids=list(planned[slot_id])))

    issues += check_preservation(facts, source, detokenized(drafts, source))
    total = sum(len(v) for v in planned.values())
    return issues, round(covered / total, 3) if total else 1.0


def draft_report(job_id: str, version: int, drafts: Iterable[SectionDraft], slot_plan: SlotPlan, source: SourceDocument,
                 template: TemplateModel, facts: ProtectedFacts) -> QualityReport:
    issues, coverage = draft_issues(drafts, slot_plan, source, template, facts)
    return QualityReport(job_id=job_id, version=version, issues=issues, unit_coverage=coverage)

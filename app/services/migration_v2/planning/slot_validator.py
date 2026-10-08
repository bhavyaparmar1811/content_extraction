"""Deterministic checks of a ``SlotPlan`` (Phase 8). Issues with a gate block plan approval.

- every ID the plan names exists, and every slot belongs to its section (``plan_checks``) → ``unaccounted_source``;
- every passage the section plan gives a target section is placed in a slot of that section, kept for a region
  choice, or flagged as unplaced                                                       → ``unaccounted_source``;
- no slot takes a passage from outside its section's scope                             → ``unaccounted_source``;
- every slot keeps the source order                                                    → ``sequence_violation``;
- every slot of every section has a mapping, and a required slot is filled, flagged as a gap,
  or covered by its "one of" group                                                     → ``missing_slot``;
- gaps, unplaced passages and slots marked for review are listed (no gate: the reviewer decides).
"""

from __future__ import annotations

import hashlib
from typing import Optional

from app.schemas.v2 import (
    Gate,
    IssueCategory,
    IssueSource,
    MappingStatus,
    QualityReport,
    SectionPlan,
    Severity,
    SlotPlan,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)

from ..plan_checks import slot_plan_problems
from .section_validator import covered_units


def _issue(key: str, severity: Severity, category: IssueCategory, message: str, gate: Optional[Gate] = None,
           target: Optional[str] = None, slot: Optional[str] = None, unit_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"slot_plan|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(
        issue_id=f"ISS-{digest}", severity=severity, category=category, source=IssueSource.DETERMINISTIC, gate=gate,
        target_section_id=target, slot_id=slot, unit_ids=unit_ids or [], message=message,
    )


def _ids(units: list[str]) -> str:
    return f"{', '.join(units[:8])}{'…' if len(units) > 8 else ''}"


def section_scope(section_plan: SectionPlan, source: SourceDocument) -> dict[str, list[str]]:
    """Target section → the substantive passages the section plan sends there (document history: all its rows)."""
    source_units = {s.section_id: [u.unit_id for u in s.units] for s in source.sections}
    by_id = {u.unit_id: u for u in source.iter_units()}
    scope: dict[str, list[str]] = {}
    for m in section_plan.mappings:
        if not m.target_section_id or m.status not in (MappingStatus.MAPPED, MappingStatus.NEEDS_REVIEW):
            continue
        scope.setdefault(m.target_section_id, []).extend(covered_units(m, source_units))
    out = {}
    for target, ids in scope.items():
        units = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        substantive = [u.unit_id for u in units if not u.is_boilerplate and (u.text.strip() or u.unit_type.value == "figure")]
        out[target] = substantive or [u.unit_id for u in units if u.text.strip()]
    return out


def validate_slot_plan(plan: SlotPlan, section_plan: SectionPlan, source: SourceDocument,
                       template: TemplateModel) -> QualityReport:
    issues: list[ValidationIssue] = []
    for problem in slot_plan_problems(plan, source, template):
        issues.append(_issue(f"ref|{problem}", Severity.HIGH, IssueCategory.STRUCTURE, problem, Gate.UNACCOUNTED_SOURCE))

    scope = section_scope(section_plan, source)
    seq = {u.unit_id: u.seq for u in source.iter_units()}
    planned = {s.target_section_id: s for s in plan.sections}
    accounted_total: set[str] = set()

    for target in template.sections:
        section = planned.get(target.section_id)
        if section is None:
            issues.append(_issue(f"nosection|{target.section_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                 f"Section '{target.heading}' has no slot plan.", Gate.MISSING_SLOT, target.section_id))
            continue
        in_scope = set(scope.get(target.section_id, []))
        slot_ids = {m.slot_id: m for m in section.slot_mappings}

        # Unit accounting inside the section.
        placed = {u for m in section.slot_mappings for u in m.source_unit_ids}
        regions = {u for r in section.region_choices for u in r.unit_ids}
        accounted = placed | regions | set(section.unplaced_unit_ids)
        accounted_total |= accounted & in_scope
        missing = sorted(in_scope - accounted, key=lambda u: seq.get(u, 0))
        if missing:
            issues.append(_issue(f"unaccounted|{target.section_id}", Severity.CRITICAL, IssueCategory.TRACEABILITY,
                                 f"'{target.heading}': {len(missing)} passage(s) are in no slot and not flagged: {_ids(missing)}",
                                 Gate.UNACCOUNTED_SOURCE, target.section_id, unit_ids=missing))
        outside = sorted((placed | regions) - in_scope, key=lambda u: seq.get(u, 0))
        if outside:
            issues.append(_issue(f"outside|{target.section_id}", Severity.HIGH, IssueCategory.TRACEABILITY,
                                 f"'{target.heading}' takes passages the section plan sends elsewhere: {_ids(outside)}; "
                                 "move content between sections in the section plan", Gate.UNACCOUNTED_SOURCE,
                                 target.section_id, unit_ids=outside))
        if section.unplaced_unit_ids:
            issues.append(_issue(f"unplaced|{target.section_id}", Severity.MEDIUM, IssueCategory.STRUCTURE,
                                 f"'{target.heading}': {len(section.unplaced_unit_ids)} passage(s) fit no slot: "
                                 f"{_ids(section.unplaced_unit_ids)}", target=target.section_id,
                                 unit_ids=list(section.unplaced_unit_ids)))

        # Slots.
        for slot in target.slots:
            m = slot_ids.get(slot.slot_id)
            ref = f"{target.key or target.section_id}.{slot.key or slot.slot_id}"
            if m is None:
                issues.append(_issue(f"noslot|{slot.slot_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                     f"Slot {ref} has no mapping.", Gate.MISSING_SLOT, target.section_id, slot.slot_id))
                continue
            if m.source_unit_ids:
                order = [seq.get(u, 0) for u in m.source_unit_ids]
                if order != sorted(order):
                    issues.append(_issue(f"order|{slot.slot_id}", Severity.HIGH, IssueCategory.ORDER,
                                         f"Slot {ref} lists passages out of source order.", Gate.SEQUENCE_VIOLATION,
                                         target.section_id, slot.slot_id))
                if m.status == MappingStatus.NEEDS_REVIEW or m.requires_human_review:
                    issues.append(_issue(f"review|{slot.slot_id}", Severity.MEDIUM, IssueCategory.STRUCTURE,
                                         f"Slot {ref} needs review: {m.note or 'marked for review'}",
                                         target=target.section_id, slot=slot.slot_id, unit_ids=list(m.source_unit_ids)))
                continue
            if m.status == MappingStatus.SOURCE_CONTENT_NOT_FOUND:
                issues.append(_issue(f"gap|{slot.slot_id}", Severity.MEDIUM, IssueCategory.STRUCTURE,
                                     f"Slot {ref}: no source content; the draft shows a gap marker for the reviewer.",
                                     target=target.section_id, slot=slot.slot_id))
                continue
            group = next((g for g in target.one_of if slot.key in g), [])
            others = [slot_ids.get(s.slot_id) for s in target.slots if s.key in group and s.slot_id != slot.slot_id]
            covered = any(o is not None and (o.source_unit_ids or o.status == MappingStatus.SOURCE_CONTENT_NOT_FOUND)
                          for o in others)
            if slot.required and not covered and target.section_id in scope:
                issues.append(_issue(f"required|{slot.slot_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                     f"Required slot {ref} is {m.status.value} but neither filled nor flagged as a gap.",
                                     Gate.MISSING_SLOT, target.section_id, slot.slot_id))

    substantive = {u for ids in scope.values() for u in ids}
    coverage = round(len(accounted_total) / len(substantive), 3) if substantive else 1.0
    return QualityReport(job_id=plan.job_id, version=plan.version, issues=issues, unit_coverage=coverage)

"""Deterministic checks of a ``SectionPlan`` (Phase 7). Issues with a gate block plan approval.

- every ID the plan names exists (``plan_checks``);
- every required target section is mapped, or flagged as having no source content  → ``missing_section``;
- every non-boilerplate source unit is placed, omitted with a reason, or flagged   → ``unaccounted_source``;
- no unit goes to two targets (unless the mapping is a ``copy``);
- merged sections keep the source order                                          → ``sequence_violation``;
- mappings still marked for review are listed (no gate: a reviewer may approve them as they are).

In a ``split`` mapping, a listed section none of whose units appear in
``unit_ids`` counts as whole; otherwise only its listed units count.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from app.schemas.v2 import (
    Gate,
    IssueCategory,
    IssueSource,
    MappingStatus,
    MappingType,
    QualityReport,
    SectionPlan,
    Severity,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)

from ..plan_checks import section_plan_problems


def _issue(key: str, severity: Severity, category: IssueCategory, message: str, gate: Optional[Gate] = None,
           target: Optional[str] = None, unit_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"section_plan|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(
        issue_id=f"ISS-{digest}", severity=severity, category=category, source=IssueSource.DETERMINISTIC, gate=gate,
        target_section_id=target, unit_ids=unit_ids or [], message=message,
    )


def _covered_units(mapping, source_units: dict[str, list[str]]) -> list[str]:
    listed = set(mapping.unit_ids)
    out: list[str] = []
    for section_id in mapping.source_section_ids:
        units = source_units.get(section_id, [])
        if mapping.mapping_type == MappingType.SPLIT and listed & set(units):
            out += [u for u in units if u in listed]
        else:
            out += units
    return out


def validate_section_plan(plan: SectionPlan, source: SourceDocument, template: TemplateModel) -> QualityReport:
    issues: list[ValidationIssue] = []
    for problem in section_plan_problems(plan, source, template):
        issues.append(_issue(f"ref|{problem}", Severity.HIGH, IssueCategory.STRUCTURE, problem, Gate.UNACCOUNTED_SOURCE))

    source_units = {s.section_id: [u.unit_id for u in s.units] for s in source.sections}
    substantive = {u.unit_id for u in source.iter_units() if not u.is_boilerplate and u.text.strip()}
    order = {s.section_id: s.section_order for s in source.sections}
    targets = {t.section_id: t for t in template.sections}

    # Required targets.
    covered_targets = {m.target_section_id for m in plan.mappings if m.target_section_id and (
        m.status in (MappingStatus.MAPPED, MappingStatus.NEEDS_REVIEW, MappingStatus.SOURCE_CONTENT_NOT_FOUND)
        and (m.source_section_ids or m.status == MappingStatus.SOURCE_CONTENT_NOT_FOUND)
    )}
    for t in template.sections:
        if t.required and t.section_id not in covered_targets:
            issues.append(_issue(f"missing|{t.section_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                                 f"Required section '{t.heading}' has no mapping and is not flagged as a gap.",
                                 Gate.MISSING_SECTION, t.section_id))

    # Unit accounting.
    placed: dict[str, list[str]] = {}
    unresolved_units: list[str] = []
    for m in plan.mappings:
        units = _covered_units(m, source_units)
        if m.mapping_type == MappingType.UNRESOLVED or (m.target_section_id is None and m.mapping_type != MappingType.OMIT):
            unresolved_units += units
            continue
        label = m.target_section_id or "omit"
        for u in units:
            if m.mapping_type == MappingType.COPY:
                continue
            placed.setdefault(u, []).append(label)
    for u, labels in placed.items():
        if len(set(labels)) > 1:
            issues.append(_issue(f"twice|{u}", Severity.HIGH, IssueCategory.TRACEABILITY,
                                 f"Unit {u} is placed in more than one target ({', '.join(sorted(set(labels)))}); use 'copy' "
                                 "if that is intended.", unit_ids=[u]))
    unaccounted = sorted((substantive - set(placed)) - set(unresolved_units))
    if unaccounted:
        issues.append(_issue(f"unaccounted|{','.join(unaccounted)}", Severity.CRITICAL, IssueCategory.TRACEABILITY,
                             f"{len(unaccounted)} source unit(s) are neither placed, omitted nor flagged: "
                             f"{', '.join(unaccounted[:8])}{'…' if len(unaccounted) > 8 else ''}",
                             Gate.UNACCOUNTED_SOURCE, unit_ids=unaccounted))
    pending = sorted(set(unresolved_units) & substantive)
    if pending:
        issues.append(_issue(f"unresolved|{','.join(pending)}", Severity.HIGH, IssueCategory.TRACEABILITY,
                             f"{len(pending)} source unit(s) have no target yet; map or omit them: "
                             f"{', '.join(pending[:8])}{'…' if len(pending) > 8 else ''}",
                             Gate.UNACCOUNTED_SOURCE, unit_ids=pending))

    # Order and review flags.
    for i, m in enumerate(plan.mappings):
        ids = [s for s in m.source_section_ids if s in order]
        if ids != sorted(ids, key=order.get):
            issues.append(_issue(f"order|{i}", Severity.HIGH, IssueCategory.ORDER,
                                 f"Mapping to {m.target_section_id or m.mapping_type.value} lists source sections out of "
                                 f"source order: {ids}.", Gate.SEQUENCE_VIOLATION, m.target_section_id))
        if m.status == MappingStatus.NEEDS_REVIEW and m.target_section_id:
            heading = targets[m.target_section_id].heading if m.target_section_id in targets else m.target_section_id
            issues.append(_issue(f"review|{m.target_section_id}", Severity.MEDIUM, IssueCategory.STRUCTURE,
                                 f"Mapping to '{heading}' needs review: {m.reason}", target=m.target_section_id))

    coverage = round(len(substantive & set(placed)) / len(substantive), 3) if substantive else 1.0
    return QualityReport(job_id=plan.job_id, version=plan.version, issues=issues, unit_coverage=coverage)


def gate_issues(report: QualityReport) -> list[ValidationIssue]:
    return report.open_gate_issues

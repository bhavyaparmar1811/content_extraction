"""Quality gates and the job's ``QualityReport`` (Phase 10, migration_plan.md §21).

The report takes the last validation round (deterministic checks and critic, after repair) and adds:

- required-slot gaps: one ``missing_slot`` issue per gap marker. Nothing is generated for a gap: a reviewer
  fills the slot or resolves the issue, which accepts it as N/A (the export drops accepted gaps, Phase 12);
- missing mandatory sections: a required template section with no draft;
- passages in no slot (the slot plan's ``unplaced_unit_ids``): one ``unaccounted_source`` issue each. They are
  not in the document; a reviewer places them or resolves the issue to accept leaving them out;
- high-risk content (``risk.py``): an open medium-or-worse issue from the checks, or a high one from the
  critic, on a high-risk passage gets the ``high_risk_unresolved`` gate; each slot whose high-risk passages
  were reworded gets one medium item for direct review;
- the reviewer's resolutions from the previous report, carried over by issue ID;
- ``gate_counts`` (open issues per hard gate) and the soft scores.

The job's final status (``final_status``):
- HUMAN_REVIEW_REQUIRED while a gate issue, or a high or critical issue, is open;
- COMPLETED_WITH_WARNINGS while a medium issue is open, or no document has been rendered yet;
- COMPLETED otherwise. Low issues (GWP style) never hold a job back.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional

from app.schemas.v2 import (
    Gate,
    IssueCategory,
    IssueSource,
    JobStatus,
    ProtectedFacts,
    QualityReport,
    RiskTag,
    SectionDraft,
    Severity,
    SlotPlan,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)

from .critic import Critic
from .risk import high_risk_units

_RANK = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}
BLOCKING = (Severity.CRITICAL, Severity.HIGH)


def _issue(key: str, severity: Severity, category: IssueCategory, message: str, gate: Optional[Gate] = None,
           target: Optional[str] = None, slot: Optional[str] = None, unit_ids: Optional[list[str]] = None,
           claim_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"gates|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(issue_id=f"ISS-{digest}", severity=severity, category=category, source=IssueSource.DETERMINISTIC,
                           gate=gate, target_section_id=target, slot_id=slot, unit_ids=unit_ids or [],
                           claim_ids=claim_ids or [], message=message)


def gap_issues(drafts: list[SectionDraft], template: TemplateModel) -> list[ValidationIssue]:
    slots = {s.slot_id: s for s in template.iter_slots()}
    out = []
    for draft in drafts:
        for slot in draft.slots:
            gaps = [c for c in slot.claims if c.is_gap_marker]
            if not gaps:
                continue
            tslot = slots.get(slot.slot_id)
            what = f"'{tslot.key or tslot.slot_id}' ({tslot.content_type.value})" if tslot else slot.slot_id
            out.append(_issue(f"gap|{slot.slot_id}", Severity.HIGH, IssueCategory.STRUCTURE,
                              f"Required slot {what} in {draft.target_section_id} has no source content. Add content, or "
                              "resolve this issue to accept the slot as N/A.", Gate.MISSING_SLOT, draft.target_section_id,
                              slot.slot_id, claim_ids=[c.claim_id for c in gaps]))
    return out


def missing_section_issues(drafts: list[SectionDraft], template: TemplateModel) -> list[ValidationIssue]:
    drafted = {d.target_section_id for d in drafts if any(s.claims for s in d.slots)}
    return [_issue(f"section|{s.section_id}", Severity.CRITICAL, IssueCategory.STRUCTURE,
                   f"Required section {s.section_id} ({s.heading}) has no draft.", Gate.MISSING_SECTION, s.section_id)
            for s in template.sections if s.required and s.section_id not in drafted]


RISK_TAG = "[high-risk:"
UNPLACED_TAG = "[unplaced]"


def unplaced_issues(slot_plan: Optional[SlotPlan], source: SourceDocument, template: TemplateModel) -> list[ValidationIssue]:
    """A source passage the slot plan put in no slot is not in the document: nothing leaves silently."""
    if slot_plan is None:
        return []
    units = {u.unit_id: u for u in source.iter_units()}
    headings = {s.section_id: s.heading for s in template.sections}
    out = []
    for section in slot_plan.sections:
        for unit_id in section.unplaced_unit_ids:
            unit = units.get(unit_id)
            what = f"{unit.unit_type.value} '{' '.join(unit.text.split())[:80]}'" if unit else unit_id
            out.append(_issue(f"unplaced|{unit_id}", Severity.HIGH, IssueCategory.TRACEABILITY,
                              f"{UNPLACED_TAG} {unit_id} ({what}) of '{headings.get(section.target_section_id, section.target_section_id)}' "
                              "is in no slot, so it is not in the document. Place it, or resolve this issue to accept "
                              "leaving it out.", Gate.UNACCOUNTED_SOURCE, section.target_section_id, unit_ids=[unit_id]))
    return out


def awaits_reviewer(issue: ValidationIssue) -> bool:
    """A gap to fill or accept, or a finding on high-risk content to confirm: a reviewer's call, not a defect."""
    return (issue.gate == Gate.MISSING_SLOT or (issue.gate == Gate.HIGH_RISK_UNRESOLVED and RISK_TAG in issue.message)
            or (issue.gate == Gate.UNACCOUNTED_SOURCE and issue.message.startswith(UNPLACED_TAG)))


def escalate(issues: list[ValidationIssue], risk: dict[str, list[RiskTag]]) -> list[ValidationIssue]:
    """Open issues on high-risk passages block completion until a reviewer resolves them."""
    out = []
    for issue in issues:
        floor = Severity.HIGH if issue.source == IssueSource.CRITIC else Severity.MEDIUM
        risky = sorted({t.value for u in issue.unit_ids for t in risk.get(u, [])})
        if issue.gate is None and not issue.resolved and risky and _RANK[issue.severity] <= _RANK[floor]:
            issue = issue.model_copy(update={
                "gate": Gate.HIGH_RISK_UNRESOLVED,
                "severity": issue.severity if _RANK[issue.severity] <= _RANK[Severity.HIGH] else Severity.HIGH,
                "message": f"{issue.message} {RISK_TAG} {', '.join(risky)}]",
            })
        out.append(issue)
    return out


def reworded_risk_items(drafts: list[SectionDraft], risk: dict[str, list[RiskTag]], critic: Critic) -> list[ValidationIssue]:
    out = []
    for draft in drafts:
        for slot in draft.slots:
            claims = [c for c in slot.claims if critic.reworded(c) and any(u in risk for u in c.source_unit_ids)]
            if not claims:
                continue
            units = list(dict.fromkeys(u for c in claims for u in c.source_unit_ids if u in risk))
            tags = sorted({t.value for u in units for t in risk[u]})
            out.append(_issue(f"risk|{slot.slot_id}|{','.join(units)}", Severity.MEDIUM, IssueCategory.PRESERVATION,
                              f"{len(claims)} reworded claim(s) in {slot.slot_id} carry high-risk content ({', '.join(tags)}): "
                              "read them against the source.", target=draft.target_section_id, slot=slot.slot_id,
                              unit_ids=units, claim_ids=[c.claim_id for c in claims]))
    return out


def carry_resolutions(issues: Iterable[ValidationIssue], previous: Optional[QualityReport]) -> list[ValidationIssue]:
    resolved = {i.issue_id: i.resolution_note for i in (previous.issues if previous else []) if i.resolved}
    return [i.model_copy(update={"resolved": True, "resolution_note": resolved[i.issue_id]})
            if i.issue_id in resolved and not i.resolved else i for i in issues]


def gate_counts(issues: Iterable[ValidationIssue]) -> dict[Gate, int]:
    counts = {g: 0 for g in Gate}
    for issue in issues:
        if issue.gate is not None and not issue.resolved:
            counts[issue.gate] += 1
    return counts


def quality_report(job_id: str, version: int, validation: QualityReport, drafts: list[SectionDraft],
                   source: SourceDocument, template: TemplateModel, facts: ProtectedFacts,
                   previous: Optional[QualityReport] = None, slot_plan: Optional[SlotPlan] = None) -> QualityReport:
    risk = high_risk_units(source, facts)
    critic = Critic(source, template)
    issues = (list(validation.issues) + gap_issues(drafts, template) + missing_section_issues(drafts, template)
              + unplaced_issues(slot_plan, source, template))
    issues = carry_resolutions(issues, previous)
    issues = escalate(issues, risk) + reworded_risk_items(drafts, risk, critic)
    issues = carry_resolutions(list({i.issue_id: i for i in issues}.values()), previous)
    issues.sort(key=lambda i: (i.resolved, i.gate is None, _RANK[i.severity], i.target_section_id or "", i.issue_id))
    reworded = [c for d in drafts for c in d.iter_claims() if critic.reworded(c)]
    scores = dict(validation.soft_scores)
    scores["reworded_share"] = round(len(reworded) / max(1, sum(1 for d in drafts for c in d.iter_claims()
                                                                if c.source_unit_ids)), 3)
    return QualityReport(job_id=job_id, version=version, issues=issues, unit_coverage=validation.unit_coverage,
                         soft_scores=scores, high_risk_units=risk, gate_counts=gate_counts(issues))


def open_issues(report: QualityReport) -> list[ValidationIssue]:
    return [i for i in report.issues if not i.resolved]


def final_status(report: QualityReport, rendered: bool) -> JobStatus:
    open_ = open_issues(report)
    if report.open_gate_issues or any(i.severity in BLOCKING for i in open_):
        return JobStatus.HUMAN_REVIEW_REQUIRED
    if not rendered or any(i.severity == Severity.MEDIUM for i in open_):
        return JobStatus.COMPLETED_WITH_WARNINGS
    return JobStatus.COMPLETED

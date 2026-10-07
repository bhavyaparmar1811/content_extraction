"""Template readiness report (Phase 3b). A migration is blocked unless the status is ``ready``.

Blocking issues come in two kinds:

- normalization: a slot has no anchor, or only a paragraph-proximity anchor.
  ``POST /templates/{id}/normalize`` fixes these.
- review: an ambiguous region no one has decided, an icon row without an
  instruction, a callout slot whose kind has no prototype box, or a config
  entry that names nothing. A human fixes these in the template config.

Warnings (palette notes, highlighted text, sections without slots) are
reported and never block.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.schemas.v2 import AnchorKind, ReadinessStatus
from app.schemas.v2.common import V2Model

from .slot_detector import DetectionResult

NORMALIZATION = "normalization"
REVIEW = "review"
WARNING = "warning"


class ReadinessIssue(V2Model):
    code: str
    severity: str = Field(description="'normalization' or 'review' (both block), or 'warning'")
    message: str
    ref: Optional[str] = Field(default=None, description="Slot ID, region ID or config key")


class AmbiguousRegionReport(V2Model):
    region_id: str
    kind: str
    section_key: Optional[str] = None
    text: str
    suggestion: str
    decision: Optional[str] = None


class ReadinessReport(V2Model):
    template_id: str
    template_version: int
    status: ReadinessStatus
    slot_count: int
    anchors: dict[str, int] = Field(default_factory=dict, description="Slot count per anchor kind")
    issues: list[ReadinessIssue] = Field(default_factory=list)
    ambiguous_regions: list[AmbiguousRegionReport] = Field(default_factory=list)

    @property
    def blocking(self) -> list[ReadinessIssue]:
        return [i for i in self.issues if i.severity != WARNING]


def assess(detection: DetectionResult, unknown_config_refs: Optional[list[str]] = None) -> ReadinessReport:
    model = detection.model
    issues: list[ReadinessIssue] = []
    anchors: dict[str, int] = {}

    for slot in model.iter_slots():
        kind = slot.anchor.kind.value if slot.anchor else "none"
        anchors[kind] = anchors.get(kind, 0) + 1
        if slot.anchor is None:
            issues.append(ReadinessIssue(code="unanchored_slot", severity=NORMALIZATION, ref=slot.slot_id,
                                         message="slot has no anchor"))
        elif slot.anchor.kind == AnchorKind.PARAGRAPH:
            issues.append(ReadinessIssue(code="paragraph_anchor", severity=NORMALIZATION, ref=slot.slot_id,
                                         message=f"slot is anchored by paragraph position ({slot.anchor.ref}) only"))

    for section in model.sections:
        for conditional in section.conditional_regions:
            if conditional.anchor is None or conditional.anchor.kind != AnchorKind.CONTENT_CONTROL:
                issues.append(ReadinessIssue(
                    code="conditional_without_control", severity=NORMALIZATION, ref=conditional.region_id,
                    message=f"conditional {conditional.kind.value} in {section.key} is not in a content control yet",
                ))

    for region in detection.unresolved:
        issues.append(ReadinessIssue(
            code="ambiguous_region", severity=REVIEW, ref=region.region_id,
            message=f"{region.kind} in {region.section_key}: decide fixed / instruction / slot "
                    f"(suggested: {region.suggestion.value}): '{region.text[:80]}'",
        ))
    for ref in detection.icons_without_instruction:
        issues.append(ReadinessIssue(code="icon_without_instruction", severity=REVIEW, ref=ref,
                                     message="icon row has no instruction text"))

    prototyped = {s.kind for s in model.callout_palette if s.prototype_table_index is not None}
    for slot in model.iter_slots():
        if slot.callout_kind is not None and slot.callout_kind not in prototyped:
            issues.append(ReadinessIssue(code="callout_without_prototype", severity=REVIEW, ref=slot.slot_id,
                                         message=f"callout kind {slot.callout_kind.value} has no prototype box"))
    for ref in unknown_config_refs or []:
        issues.append(ReadinessIssue(code="unknown_config_ref", severity=REVIEW, ref=ref,
                                     message="template config names a slot or section that was not detected"))

    for message in detection.issues:
        issues.append(ReadinessIssue(code="detector_note", severity=WARNING, message=message))
    for section in model.sections:
        if section.required and not section.slots:
            issues.append(ReadinessIssue(code="section_without_slots", severity=WARNING, ref=section.section_id,
                                         message=f"required section '{section.heading}' has no slot"))

    if any(i.severity == REVIEW for i in issues):
        status = ReadinessStatus.NEEDS_REVIEW
    elif any(i.severity == NORMALIZATION for i in issues):
        status = ReadinessStatus.NEEDS_NORMALIZATION
    else:
        status = ReadinessStatus.READY

    return ReadinessReport(
        template_id=model.template_id,
        template_version=model.template_version,
        status=status,
        slot_count=sum(1 for _ in model.iter_slots()),
        anchors=anchors,
        issues=issues,
        ambiguous_regions=[
            AmbiguousRegionReport(
                region_id=a.region_id, kind=a.kind, section_key=a.section_key, text=a.text,
                suggestion=a.suggestion.value, decision=a.decision.value if a.decision else None,
            )
            for a in detection.ambiguous
        ],
    )

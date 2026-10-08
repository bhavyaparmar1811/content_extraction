"""Section plan (Level 1) and slot plan (Level 2)."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field, model_validator

from .common import CalloutKind, V2Model


class MappingType(str, Enum):
    ONE_TO_ONE = "one_to_one"
    MERGE = "merge"
    SPLIT = "split"
    MOVE = "move"
    COPY = "copy"
    OMIT = "omit"
    UNRESOLVED = "unresolved"


class MappingStatus(str, Enum):
    MAPPED = "mapped"
    SOURCE_CONTENT_NOT_FOUND = "source_content_not_found"
    NOT_APPLICABLE = "not_applicable"
    UNMAPPED_SOURCE_CONTENT = "unmapped_source_content"
    CONFLICTING_SOURCE = "conflicting_source"
    NEEDS_REVIEW = "needs_review"


class OrderingRule(str, Enum):
    PRESERVE_SOURCE_ORDER = "preserve_source_order"
    TEMPLATE_ORDER = "template_order"


class PlanOrigin(str, Enum):
    LLM = "llm"
    HUMAN = "human"
    RULE = "rule"  # deterministic planner, no LLM involved


class MappingOrigin(str, Enum):
    """Who decided one mapping: the deterministic rules, the LLM (a correction), or a reviewer."""

    RULE = "rule"
    LLM = "llm"
    HUMAN = "human"


# ── Level 1 ────────────────────────────────────────────────────────────

class SectionMapping(V2Model):
    target_section_id: Optional[str] = Field(default=None, description="None for omitted or unmapped source content")
    source_section_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(
        default_factory=list, description="Required for split mappings: which units of the source go to this target"
    )
    mapping_type: MappingType
    ordering_rule: OrderingRule = OrderingRule.PRESERVE_SOURCE_ORDER
    status: MappingStatus = MappingStatus.MAPPED
    reason: str = ""
    justification: Optional[str] = Field(default=None, description="Required for omit")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    origin: MappingOrigin = MappingOrigin.RULE

    @model_validator(mode="after")
    def _rules(self) -> "SectionMapping":
        if self.mapping_type == MappingType.SPLIT and not self.unit_ids:
            raise ValueError("split mappings must list the unit_ids that go to this target")
        if self.mapping_type == MappingType.OMIT and not self.justification:
            raise ValueError("omit mappings need a justification")
        if self.mapping_type != MappingType.OMIT and self.status == MappingStatus.MAPPED and not self.target_section_id:
            raise ValueError("mapped section mappings need a target_section_id")
        return self


class SectionPlan(V2Model):
    job_id: str
    version: int = Field(ge=1)
    origin: PlanOrigin = PlanOrigin.LLM
    mappings: list[SectionMapping] = Field(default_factory=list)
    approved_by: Optional[str] = None
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    token_usage: dict[str, int] = Field(default_factory=dict, description="e.g. input_tokens, output_tokens, calls")


# ── Level 2 ────────────────────────────────────────────────────────────

class MigrationAction(str, Enum):
    EXTRACT_AND_REWRITE = "extract_and_rewrite"
    EXTRACT_AND_CONSOLIDATE = "extract_and_consolidate"
    REWRITE_AS_ORDERED_PROCEDURE = "rewrite_as_ordered_procedure"
    COPY_VERBATIM = "copy_verbatim"
    NONE = "none"


class SlotMapping(V2Model):
    slot_id: str
    source_unit_ids: list[str] = Field(default_factory=list)
    extraction_scope: list[str] = Field(default_factory=list, description="e.g. ['actor', 'responsibility']")
    migration_action: MigrationAction = MigrationAction.EXTRACT_AND_REWRITE
    ordering_rule: OrderingRule = OrderingRule.PRESERVE_SOURCE_ORDER
    status: MappingStatus = MappingStatus.MAPPED
    requires_human_review: bool = False
    note: Optional[str] = None
    origin: MappingOrigin = MappingOrigin.RULE
    rule_ids: list[str] = Field(
        default_factory=list,
        description="GWP rules the drafter applies to this slot, selected from the job's rule set by content type",
    )

    @model_validator(mode="after")
    def _rules(self) -> "SlotMapping":
        if self.status == MappingStatus.MAPPED and not self.source_unit_ids:
            raise ValueError(f"slot {self.slot_id} is mapped but has no source_unit_ids")
        if self.status == MappingStatus.SOURCE_CONTENT_NOT_FOUND:
            if self.source_unit_ids:
                raise ValueError(f"slot {self.slot_id} is source_content_not_found but lists source units")
            self.migration_action = MigrationAction.NONE
            self.requires_human_review = True
        return self


class AssignmentOrigin(str, Enum):
    RULE = "rule"  # deterministic: warning -> attention, note -> explanation
    LLM = "llm"
    HUMAN = "human"


class CalloutAssignment(V2Model):
    """Promotes source units into a callout box of one palette kind."""

    unit_ids: list[str] = Field(min_length=1)
    kind: CalloutKind
    origin: AssignmentOrigin = AssignmentOrigin.RULE
    reason: str = Field(min_length=1)


class RegionChoice(V2Model):
    """A source line that answers one of the template's conditional regions instead of filling a slot.

    E.g. the source lead-in "This SOP is applicable:" answers the inline choice
    "This Directive/SOP/Work Instruction/Guidance is applicable:" with "SOP".
    """

    region_id: str
    unit_ids: list[str] = Field(min_length=1)
    choice: Optional[str] = Field(default=None, description="The option the source uses, e.g. 'SOP'")


class SectionSlotPlan(V2Model):
    target_section_id: str
    source_section_ids: list[str] = Field(default_factory=list)
    slot_mappings: list[SlotMapping] = Field(default_factory=list)
    unplaced_unit_ids: list[str] = Field(
        default_factory=list, description="Units in scope that fit no slot; flagged for plan review"
    )
    callout_assignments: list[CalloutAssignment] = Field(default_factory=list)
    region_choices: list[RegionChoice] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="For the reviewer: unplaced units, LLM doubts")

    @model_validator(mode="after")
    def _unit_in_one_callout(self) -> "SectionSlotPlan":
        seen: set[str] = set()
        for assignment in self.callout_assignments:
            dup = seen.intersection(assignment.unit_ids)
            if dup:
                raise ValueError(f"units assigned to more than one callout: {sorted(dup)}")
            seen.update(assignment.unit_ids)
        return self

    def callout_kind_for(self, unit_id: str) -> Optional[CalloutKind]:
        for assignment in self.callout_assignments:
            if unit_id in assignment.unit_ids:
                return assignment.kind
        return None


class SlotPlan(V2Model):
    job_id: str
    version: int = Field(ge=1)
    origin: PlanOrigin = PlanOrigin.LLM
    sections: list[SectionSlotPlan] = Field(default_factory=list)
    approved_by: Optional[str] = None
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    token_usage: dict[str, int] = Field(default_factory=dict, description="e.g. input_tokens, output_tokens, calls")
    gwp_guide_id: Optional[str] = Field(default=None, description="Rule set the rule_ids come from; 'BASELINE' without a GWP")

    def section(self, target_section_id: str) -> Optional[SectionSlotPlan]:
        return next((s for s in self.sections if s.target_section_id == target_section_id), None)

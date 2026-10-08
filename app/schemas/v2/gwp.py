"""Good Writing Practice (GWP) rule catalog."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import Field, model_validator

from .common import ContentType, Severity, V2Model


class RuleCategory(str, Enum):
    STRUCTURAL = "STR"      # affects placement, used by planners
    STYLE = "STY"           # language and style, used by the drafter
    PRESERVATION = "PRES"   # content preservation, used by drafter and validator
    FORMATTING = "FMT"      # used during document assembly


class RuleCheck(str, Enum):
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"
    NONE = "none"


class RuleStatus(str, Enum):
    CANDIDATE = "candidate"  # extracted by the LLM, not yet reviewed
    APPROVED = "approved"
    REJECTED = "rejected"


class RuleOrigin(str, Enum):
    GUIDE = "guide"        # extracted from the GWP document; cites its source units
    BASELINE = "baseline"  # built-in migration invariant (e.g. preserve numbers), not from the guide
    MANUAL = "manual"      # added by a reviewer


class GwpRule(V2Model):
    rule_id: str = Field(description="e.g. 'STY-001'")
    category: RuleCategory
    text: str
    applies_to_content_types: list[ContentType] = Field(
        default_factory=list, description="Empty means the rule applies to every content type"
    )
    severity: Severity = Severity.MEDIUM
    check: RuleCheck = RuleCheck.SEMANTIC
    params: dict[str, Any] = Field(default_factory=dict)
    source_unit_ids: list[str] = Field(default_factory=list, description="Where in the GWP document the rule came from")
    status: RuleStatus = RuleStatus.CANDIDATE
    origin: RuleOrigin = RuleOrigin.GUIDE

    @model_validator(mode="after")
    def _id_matches_category(self) -> "GwpRule":
        if not self.rule_id.startswith(f"{self.category.value}-"):
            raise ValueError(f"rule_id {self.rule_id!r} must start with '{self.category.value}-'")
        return self

    def applies_to(self, content_type: ContentType) -> bool:
        return not self.applies_to_content_types or content_type in self.applies_to_content_types


class GwpRuleSet(V2Model):
    guide_id: str
    version: int = Field(ge=1)
    source_file: Optional[str] = None
    rules: list[GwpRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "GwpRuleSet":
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule_id in GwpRuleSet")
        return self

    def select(
        self,
        categories: list[RuleCategory],
        content_types: Optional[list[ContentType]] = None,
        approved_only: bool = True,
    ) -> list[GwpRule]:
        """Rules for the given categories that apply to any of the content types."""
        selected = []
        for rule in self.rules:
            if rule.category not in categories:
                continue
            if approved_only and rule.status != RuleStatus.APPROVED:
                continue
            if content_types and not any(rule.applies_to(ct) for ct in content_types):
                continue
            selected.append(rule)
        return selected


class SkippedGuidance(V2Model):
    """Guide text that yields no rule, e.g. advice about the authoring process itself."""

    source_unit_ids: list[str] = Field(default_factory=list)
    reason: str


class DroppedCandidate(V2Model):
    text: str
    reason: str


class GwpExtractionReport(V2Model):
    """What a reviewer needs next to the candidate rules: coverage and every automatic correction."""

    guide_id: str
    version: int = Field(ge=1)
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    input_unit_ids: list[str] = Field(default_factory=list, description="Guide units shown to the extractor")
    uncovered_unit_ids: list[str] = Field(
        default_factory=list, description="Input units neither cited by a rule nor skipped with a reason"
    )
    skipped: list[SkippedGuidance] = Field(default_factory=list)
    dropped: list[DroppedCandidate] = Field(default_factory=list, description="Candidates removed, e.g. no valid source")
    downgraded: dict[str, list[str]] = Field(
        default_factory=dict, description="rule_id -> why its deterministic params were invalid (now semantic)"
    )

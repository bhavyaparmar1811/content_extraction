"""Protected facts, validation issues and the quality report."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field

from .common import Severity, V2Model


class Modality(str, Enum):
    MANDATORY = "mandatory"        # must, shall, is required to
    PROHIBITION = "prohibition"    # must not, shall not
    RECOMMENDED = "recommended"    # should
    PERMITTED = "permitted"        # may, can
    FUTURE = "future"              # will


class FactKind(str, Enum):
    NUMBER = "number"
    PERCENTAGE = "percentage"
    DATE = "date"
    DURATION = "duration"
    FREQUENCY = "frequency"
    DEADLINE = "deadline"
    ROLE = "role"
    REFERENCE = "reference"  # document, form or system ID


class ProtectedValue(V2Model):
    kind: FactKind
    raw: str = Field(description="As written, e.g. 'five business days'")
    normalized: str = Field(description="Canonical form used for comparison, e.g. '5 business_day'")
    unit_id: str


class ProtectedObligation(V2Model):
    unit_id: str
    statement: str
    modality: Modality
    actor: Optional[str] = None


class ProtectedFacts(V2Model):
    job_id: str
    values: list[ProtectedValue] = Field(default_factory=list)
    obligations: list[ProtectedObligation] = Field(default_factory=list)
    approved_terms: dict[str, str] = Field(default_factory=dict, description="Full term -> abbreviation")
    roles: list[str] = Field(default_factory=list)


class IssueCategory(str, Enum):
    STRUCTURE = "structure"
    TRACEABILITY = "traceability"
    PRESERVATION = "preservation"
    ORDER = "order"
    FORMATTING = "formatting"
    CROSS_REFERENCE = "cross_reference"
    SEMANTIC = "semantic"
    CONSISTENCY = "consistency"


class Gate(str, Enum):
    """Hard gates from migration_plan.md section 21. An open issue with a gate blocks completion."""

    UNSUPPORTED_CLAIM = "unsupported_claim"
    NUMERICAL_CHANGE = "numerical_change"
    MISSING_SECTION = "missing_section"
    MISSING_SLOT = "missing_slot"
    BROKEN_CROSS_REFERENCE = "broken_cross_reference"
    UNACCOUNTED_SOURCE = "unaccounted_source"
    HIGH_RISK_UNRESOLVED = "high_risk_unresolved"
    SEQUENCE_VIOLATION = "sequence_violation"


class IssueSource(str, Enum):
    DETERMINISTIC = "deterministic"
    CRITIC = "critic"
    HUMAN = "human"


class ValidationIssue(V2Model):
    issue_id: str
    severity: Severity
    category: IssueCategory
    source: IssueSource = IssueSource.DETERMINISTIC
    gate: Optional[Gate] = None
    target_section_id: Optional[str] = None
    slot_id: Optional[str] = None
    claim_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    message: str
    resolved: bool = False
    resolution_note: Optional[str] = None


class QualityReport(V2Model):
    job_id: str
    version: int = Field(ge=1)
    issues: list[ValidationIssue] = Field(default_factory=list)
    unit_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    soft_scores: dict[str, float] = Field(default_factory=dict)

    @property
    def open_gate_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.gate is not None and not i.resolved]

    @property
    def gates_passed(self) -> bool:
        return not self.open_gate_issues

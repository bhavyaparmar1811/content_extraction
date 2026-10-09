"""Migration job, per-section state and versioned artifacts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import Field

from .common import V2Model


class JobStatus(str, Enum):
    """Job lifecycle (migration_plan.md section 22)."""

    PENDING = "PENDING"
    PARSING = "PARSING"
    PLANNING_SECTIONS = "PLANNING_SECTIONS"
    SECTION_PLAN_REVIEW_PENDING = "SECTION_PLAN_REVIEW_PENDING"
    PLANNING_SLOTS = "PLANNING_SLOTS"
    SLOT_PLAN_REVIEW_PENDING = "SLOT_PLAN_REVIEW_PENDING"
    DRAFTING = "DRAFTING"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    ASSEMBLING = "ASSEMBLING"
    RECONCILING = "RECONCILING"
    RENDERING = "RENDERING"
    QUALITY_REVIEW = "QUALITY_REVIEW"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    MANUALLY_EDITED = "MANUALLY_EDITED"
    FAILED_TECHNICAL = "FAILED_TECHNICAL"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.FAILED_TECHNICAL, JobStatus.CANCELLED}
)


class JobMode(str, Enum):
    REVIEW = "review"  # pause for section-plan and slot-plan approval
    AUTO = "auto"


class SectionStatus(str, Enum):
    PENDING = "pending"
    SLOT_PLANNED = "slot_planned"
    DRAFTED = "drafted"
    VALIDATED = "validated"
    REPAIRING = "repairing"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"


class SectionState(V2Model):
    target_section_id: str
    status: SectionStatus = SectionStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    last_error: Optional[str] = None


class ArtifactKind(str, Enum):
    SOURCE_MODEL = "source_model"
    TEMPLATE_MODEL = "template_model"
    GWP_RULES = "gwp_rules"
    PROTECTED_FACTS = "protected_facts"
    SECTION_PLAN = "section_plan"
    SLOT_PLAN = "slot_plan"
    SECTION_DRAFT = "section_draft"
    ASSEMBLED_DRAFT = "assembled_draft"
    NUMBER_MAP = "number_map"
    QUALITY_REPORT = "quality_report"
    DOCX = "docx"
    RENDER_REPORT = "render_report"
    TRACEABILITY = "traceability"


class ArtifactRef(V2Model):
    kind: ArtifactKind
    version: int = Field(ge=1)
    path: str
    sha256: str
    scope: Optional[str] = Field(default=None, description="e.g. target_section_id for per-section artifacts")
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None


class MigrationJob(V2Model):
    job_id: str
    sop_record_id: int
    template_id: str
    template_version: int = Field(ge=1)
    gwp_id: Optional[str] = None
    gwp_version: Optional[int] = Field(default=None, ge=1)
    mode: JobMode = JobMode.REVIEW
    status: JobStatus = JobStatus.PENDING
    sections: list[SectionState] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    error: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def latest_artifact(self, kind: ArtifactKind, scope: Optional[str] = None) -> Optional[ArtifactRef]:
        matches = [a for a in self.artifacts if a.kind == kind and a.scope == scope]
        return max(matches, key=lambda a: a.version, default=None)

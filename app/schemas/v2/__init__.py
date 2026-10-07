"""v2 migration contracts. See docs/migration_v2/CONTRACTS.md."""

from .common import SCHEMA_VERSION, CalloutKind, ContentType, HexColor, Severity, V2Model
from .draft import Claim, DraftOrigin, EvidenceSpan, SectionDraft, SlotDraft
from .gwp import (
    DroppedCandidate,
    GwpExtractionReport,
    GwpRule,
    GwpRuleSet,
    RuleCategory,
    RuleCheck,
    RuleOrigin,
    RuleStatus,
    SkippedGuidance,
)
from .job import (
    TERMINAL_STATUSES,
    ArtifactKind,
    ArtifactRef,
    JobMode,
    JobStatus,
    MigrationJob,
    SectionState,
    SectionStatus,
)
from .plans import (
    AssignmentOrigin,
    CalloutAssignment,
    MappingOrigin,
    MappingStatus,
    MappingType,
    MigrationAction,
    OrderingRule,
    PlanOrigin,
    SectionMapping,
    SectionPlan,
    SectionSlotPlan,
    SlotMapping,
    SlotPlan,
)
from .quality import (
    FactKind,
    Gate,
    IssueCategory,
    IssueSource,
    Modality,
    ProtectedFacts,
    ProtectedObligation,
    ProtectedValue,
    QualityReport,
    ValidationIssue,
)
from .refs import (
    REF_TOKEN_PATTERN,
    CrossReference,
    NumberMap,
    NumberMapEntry,
    RefKind,
    RefResolution,
    find_ref_tokens,
    make_ref_token,
)
from .source import (
    AssetKind,
    SourceAsset,
    RelationType,
    SourceDocument,
    SourceLocation,
    SourceSection,
    SourceUnit,
    TableCell,
    TableRef,
    UnitRelation,
    UnitType,
    compute_content_hash,
)
from .template import (
    AnchorKind,
    CalloutLayout,
    CalloutOrigin,
    CalloutStyle,
    ConditionalKind,
    ConditionalRegion,
    FormattingProfile,
    InstructionBehavior,
    ReadinessStatus,
    SlotAnchor,
    SlotIcon,
    TargetSection,
    TargetSlot,
    TemplateModel,
)

__all__ = [name for name in dir() if not name.startswith("_")]

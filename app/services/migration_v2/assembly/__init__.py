"""Whole-document assembly (Phase 12): the section drafts in target order, numbered, with references resolved.

``assemble`` gives the ``AssembledDocument`` (what the renderer renders), the ``NumberMap`` and the reference issues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.v2 import (
    AssembledDocument,
    NumberMap,
    SectionDraft,
    SectionPlan,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)

from .numbers import bookmark_name, build_number_map, present_sections
from .xref_resolver import relative_issues, resolve_refs

__all__ = ["Assembly", "assemble", "bookmark_name", "build_number_map", "present_sections"]


@dataclass
class Assembly:
    document: AssembledDocument
    number_map: NumberMap
    issues: list[ValidationIssue] = field(default_factory=list)


def assemble(template: TemplateModel, drafts: Iterable[SectionDraft], source: SourceDocument,
             section_plan: Optional[SectionPlan] = None, *, job_id: str, version: int = 1,
             number_map_version: Optional[int] = None, accepted_gaps: Iterable[str] = ()) -> Assembly:
    order = {s.section_id: s.display_order for s in template.sections}
    drafts = sorted(drafts, key=lambda d: order.get(d.target_section_id, 10 ** 6))
    accepted = list(accepted_gaps)
    number_map = build_number_map(template, drafts, source, section_plan, accepted, job_id=job_id,
                                  version=number_map_version or version)
    refs, issues = resolve_refs(drafts, number_map, source)
    issues += relative_issues(drafts, template, source)
    document = AssembledDocument(
        job_id=job_id, version=version, section_order=present_sections(template, drafts, accepted),
        sections_removed=list(number_map.sections_removed),
        draft_versions={d.target_section_id: d.version for d in drafts}, refs=refs,
    )
    return Assembly(document, number_map, issues)

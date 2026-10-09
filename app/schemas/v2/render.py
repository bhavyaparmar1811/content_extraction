"""What the Word renderer did with each slot, conditional region and section (Phase 11)."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field

from .common import V2Model


class RenderMode(str, Enum):
    """``review``: gap markers are visible and slot content controls stay in place.
    ``final``: reviewer-accepted gaps are removed with their instructions and the content controls are unwrapped;
    refused while any gap is unresolved."""

    REVIEW = "review"
    FINAL = "final"


class SlotOutcome(str, Enum):
    FILLED = "filled"
    GAP_MARKER = "gap_marker"                      # required slot without source content (review draft only)
    REMOVED_EMPTY = "removed_empty"                # optional slot without content: anchor and instruction removed
    REMOVED_ACCEPTED_GAP = "removed_accepted_gap"  # final export: a gap the reviewer accepted as N/A
    NO_ANCHOR = "no_anchor"                        # the anchor was not found in the template file


class SlotRender(V2Model):
    slot_id: str
    outcome: SlotOutcome
    claims: int = Field(default=0, ge=0)
    tables: int = Field(default=0, ge=0, description="Tables written for the slot (template table and source-shaped ones)")
    callout_boxes: int = Field(default=0, ge=0)
    note: Optional[str] = None


class RegionOutcome(str, Enum):
    CHOICE_APPLIED = "choice_applied"  # inline choice: "This Directive/SOP/...:" → "This SOP:"
    KEPT = "kept"                      # block region kept, its text made plain
    REMOVED = "removed"


class RegionRender(V2Model):
    region_id: str
    outcome: RegionOutcome
    choice: Optional[str] = None
    note: Optional[str] = None


class RenderReport(V2Model):
    job_id: str
    version: int = Field(ge=1)
    mode: RenderMode
    template_file: str
    draft_versions: dict[str, int] = Field(default_factory=dict, description="target_section_id → rendered draft version")
    slots: list[SlotRender] = Field(default_factory=list)
    regions: list[RegionRender] = Field(default_factory=list)
    sections_removed: list[str] = Field(default_factory=list, description="Optional sections without content")
    warnings: list[str] = Field(default_factory=list, description="For the reviewer: tables in their own shape, missing figures...")
    problems: list[str] = Field(default_factory=list, description="Post-render check failures: the document is not as planned")
    toc_entries: int = Field(default=0, description="Entries written into the table of contents (the rendered headings)")
    toc_page_numbers: bool = Field(default=False, description="TOC page numbers measured by Word; else Word fills them on opening")

    @property
    def ok(self) -> bool:
        return not self.problems

    def slot(self, slot_id: str) -> Optional[SlotRender]:
        return next((s for s in self.slots if s.slot_id == slot_id), None)

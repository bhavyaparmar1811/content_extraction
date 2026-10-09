"""Traceability (Phase 12): every claim of the migrated document back to the source passages it comes from."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import Field

from .common import V2Model


class TraceRow(V2Model):
    """One claim and one source passage it cites (a claim citing two passages is two rows; a gap marker is one row
    with no passage)."""

    claim_id: str
    target_section_id: str
    target_number: Optional[str] = Field(default=None, description="The chapter or heading that holds the claim, e.g. '6.2'")
    slot_id: str
    kind: str
    text: str = Field(description="The claim as the document shows it (references resolved)")
    draft_version: int
    draft_origin: str
    rule_ids_applied: list[str] = Field(default_factory=list)
    unit_id: Optional[str] = None
    part: Optional[str] = Field(default=None, description="The cited part of the passage, when it was split between slots")
    source_section_id: Optional[str] = None
    source_section_number: Optional[str] = None
    source_section_heading: Optional[str] = None
    unit_type: Optional[str] = None
    page: Optional[int] = None
    paragraph_index: Optional[int] = None
    xml_path: Optional[str] = None
    gap_marker: bool = False


class Traceability(V2Model):
    job_id: str
    source_file: str
    generated_at: datetime
    rows: list[TraceRow] = Field(default_factory=list)

"""Drafted content: claims with source evidence, grouped by slot and section."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field, model_validator

from .common import CalloutKind, V2Model
from .refs import find_ref_tokens
from .template import FormattingProfile


class EvidenceSpan(V2Model):
    unit_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class Claim(V2Model):
    """One atomic statement in the migrated document.

    Every claim must cite the source units it came from. Gap markers are the
    only claims allowed without sources.
    """

    claim_id: str
    text: str
    source_unit_ids: list[str] = Field(default_factory=list)
    spans: list[EvidenceSpan] = Field(default_factory=list)
    rule_ids_applied: list[str] = Field(default_factory=list)
    derived_from_claim_ids: list[str] = Field(
        default_factory=list, description="Set when a summary slot reuses claims of the primary procedure slot"
    )
    is_gap_marker: bool = False
    list_level: int = Field(default=0, ge=0)
    callout_kind: Optional[CalloutKind] = Field(
        default=None, description="Adjacent claims of the same kind render in one callout box"
    )

    @model_validator(mode="after")
    def _must_cite(self) -> "Claim":
        if not self.is_gap_marker and not self.source_unit_ids:
            raise ValueError(f"claim {self.claim_id} has no source_unit_ids")
        return self

    @property
    def ref_targets(self) -> list[str]:
        return find_ref_tokens(self.text)


class SlotDraft(V2Model):
    slot_id: str
    rendering: FormattingProfile = FormattingProfile.PARAGRAPHS
    claims: list[Claim] = Field(default_factory=list)


class DraftOrigin(str, Enum):
    LLM = "llm"
    REPAIR = "repair"
    HUMAN = "human"


class SectionDraft(V2Model):
    target_section_id: str
    version: int = Field(ge=1)
    origin: DraftOrigin = DraftOrigin.LLM
    slots: list[SlotDraft] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    prompt_version: Optional[str] = None
    model: Optional[str] = None

    def iter_claims(self):
        for slot in self.slots:
            yield from slot.claims

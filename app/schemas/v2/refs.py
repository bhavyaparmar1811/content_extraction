"""Cross-references between parts of a document.

Source references are detected at extraction time. The drafter carries them as
``{{ref:<source_id>}}`` tokens, and the assembler resolves them through a
``NumberMap`` built after the whole document is drafted.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import Field

from .common import V2Model


REF_TOKEN_PATTERN = re.compile(r"\{\{ref:([A-Za-z0-9._\-]+)\}\}")


def make_ref_token(source_id: str) -> str:
    return f"{{{{ref:{source_id}}}}}"


def find_ref_tokens(text: str) -> list[str]:
    """Return the source IDs referenced by tokens in ``text``, in order."""
    return REF_TOKEN_PATTERN.findall(text)


class RefKind(str, Enum):
    SECTION = "section"
    STEP = "step"
    TABLE = "table"
    FIGURE = "figure"
    APPENDIX = "appendix"
    EXTERNAL_DOC = "external_doc"
    RELATIVE = "relative"  # "above", "below", "previous step"


class RefResolution(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    EXTERNAL = "external"


class CrossReference(V2Model):
    ref_id: str
    from_unit_id: str
    raw_text: str = Field(description="Reference phrase exactly as written in the source")
    ref_kind: RefKind
    target: Optional[str] = Field(
        default=None,
        description="Source section/unit ID, or external document ID when ref_kind is external_doc",
    )
    resolution: RefResolution = RefResolution.UNRESOLVED
    direction: Optional[str] = Field(default=None, description="'before' or 'after' for relative references")


class NumberKind(str, Enum):
    SECTION = "section"  # a template chapter heading ("7")
    HEADING = "heading"  # a source sub-heading kept inside a chapter ("6.1.2")
    UNIT = "unit"        # a passage: numbered by the heading or chapter that holds it, plus its entry or step number


class NumberMapEntry(V2Model):
    source_id: str = Field(description="Source section ID, unit ID, or a template section ID for a chapter")
    target_section_id: str
    target_number: str = Field(description="Final display number, e.g. '3', '3.2', 'step 4', 'Table 2'")
    bookmark: Optional[str] = Field(default=None, description="Word bookmark name used for REF fields")
    kind: NumberKind = NumberKind.SECTION
    claim_id: Optional[str] = Field(default=None, description="The claim that holds it (a heading, row or step)")
    item_number: Optional[str] = Field(default=None, description="A unit's own number: the 'No.' cell of a list row, or its step")
    exact: bool = Field(default=True, description="False when the source part has no place of its own: the number is "
                                                  "its holder's (merged into a larger section, or split)")
    note: Optional[str] = None


class NumberMap(V2Model):
    job_id: Optional[str] = None
    version: Optional[int] = Field(default=None, ge=1)
    entries: list[NumberMapEntry] = Field(default_factory=list)
    sections_removed: list[str] = Field(default_factory=list, description="Optional template sections without content")

    def lookup(self, source_id: str) -> Optional[NumberMapEntry]:
        return next((e for e in self.entries if e.source_id == source_id), None)


class RefStatus(str, Enum):
    RESOLVED = "resolved"      # the target has its own number in the new document
    MERGED = "merged"          # the target has no place of its own: the reference points to what holds it now
    UNRESOLVED = "unresolved"  # the target was omitted or not drafted: a reviewer must fix the reference


class ResolvedRef(V2Model):
    """One ``{{ref:...}}`` token of one claim, with what the document shows for it."""

    claim_id: str
    target: str = Field(description="The token's source ID")
    source_phrase: str = Field(description="The reference as written in the source, e.g. 'Chapter 7, no. 1'")
    text: str = Field(description="What the new document shows, e.g. 'Chapter 7, no. 1'")
    number: Optional[str] = Field(default=None, description="The new number shown inside the text (a REF field)")
    number_at: Optional[int] = Field(default=None, ge=0, description="Where ``number`` starts in ``text``")
    bookmark: Optional[str] = Field(default=None, description="The REF field's bookmark; none: plain text")
    status: RefStatus = RefStatus.RESOLVED
    note: Optional[str] = None


class AssembledDocument(V2Model):
    """The whole-document draft model (Phase 12): the section drafts in target order, numbered, references resolved.

    Rendering starts from it; the drafts themselves stay the per-section artifacts.
    """

    job_id: str
    version: int = Field(ge=1)
    section_order: list[str] = Field(default_factory=list, description="Target sections present, in document order")
    sections_removed: list[str] = Field(default_factory=list)
    draft_versions: dict[str, int] = Field(default_factory=dict, description="target_section_id → draft version used")
    refs: list[ResolvedRef] = Field(default_factory=list)

    def ref(self, claim_id: str, target: str) -> Optional[ResolvedRef]:
        return next((r for r in self.refs if r.claim_id == claim_id and r.target == target), None)

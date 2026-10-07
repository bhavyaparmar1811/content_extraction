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


class NumberMapEntry(V2Model):
    source_id: str
    target_section_id: str
    target_number: str = Field(description="Final display number, e.g. '3', '3.2', 'step 4', 'Table 2'")
    bookmark: Optional[str] = Field(default=None, description="Word bookmark name used for REF fields")


class NumberMap(V2Model):
    entries: list[NumberMapEntry] = Field(default_factory=list)

    def lookup(self, source_id: str) -> Optional[NumberMapEntry]:
        return next((e for e in self.entries if e.source_id == source_id), None)

"""Cross-reference tokens in drafted text.

An internal reference ("see Chapter 7, no. 1", "described in 6.2") is replaced
by ``{{ref:<source id>}}`` before any drafting, so no literal section or step
number reaches a claim; the assembler resolves the token through the
``NumberMap`` once the whole document is numbered (Phase 12). References to
other documents (``external_doc``) stay as written: they are protected values.

For the Phase 6 checks, ``detokenized`` puts the source wording back, so a
number that only lived inside a reference ("6.2") is not reported as lost.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from app.schemas.v2 import (
    CrossReference,
    RefKind,
    RefResolution,
    SectionDraft,
    SourceDocument,
    SourceUnit,
    find_ref_tokens,
    make_ref_token,
)
from app.schemas.v2.refs import REF_TOKEN_PATTERN


def internal_refs(source: SourceDocument) -> dict[str, list[CrossReference]]:
    """unit_id → its resolved references to parts of this document."""
    out: dict[str, list[CrossReference]] = defaultdict(list)
    for ref in source.cross_references:
        if ref.ref_kind != RefKind.EXTERNAL_DOC and ref.resolution == RefResolution.RESOLVED and ref.target:
            out[ref.from_unit_id].append(ref)
    return out


def tokenize(unit: SourceUnit, refs: Iterable[CrossReference]) -> str:
    """The unit's text with each internal reference phrase replaced by its token.

    Longest phrases first; one occurrence per reference, so a phrase cited twice is replaced twice.
    """
    text = unit.text or ""
    for ref in sorted(refs, key=lambda r: -len(r.raw_text or "")):
        if ref.raw_text:
            token = make_ref_token(ref.target)
            text = re.sub(re.escape(ref.raw_text), lambda _m: token, text, count=1, flags=re.I)
    return text


def tokens_of(text: str) -> list[str]:
    return find_ref_tokens(text or "")


def raw_text_by_target(source: SourceDocument) -> dict[tuple[str, str], str]:
    """(from_unit_id, target) → the reference phrase as written in the source."""
    return {(r.from_unit_id, r.target): r.raw_text for refs in internal_refs(source).values() for r in refs}


def detokenize(text: str, unit_ids: Iterable[str], phrases: dict[tuple[str, str], str]) -> str:
    unit_ids = list(unit_ids)

    def back(match: re.Match) -> str:
        target = match.group(1)
        return next((phrases[(u, target)] for u in unit_ids if (u, target) in phrases), target)

    return REF_TOKEN_PATTERN.sub(back, text or "")


def detokenized(drafts: Iterable[SectionDraft], source: SourceDocument) -> list[SectionDraft]:
    """Copies of the drafts with reference tokens turned back into the source's wording (for comparisons)."""
    phrases = raw_text_by_target(source)
    out = []
    for draft in drafts:
        copy = draft.model_copy(deep=True)
        for claim in copy.iter_claims():
            if REF_TOKEN_PATTERN.search(claim.text):
                claim.text = detokenize(claim.text, claim.source_unit_ids, phrases)
        out.append(copy)
    return out

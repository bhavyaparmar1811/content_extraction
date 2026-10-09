"""Bounded drafting memory: what every drafter call must agree on, cut to the passages in the call.

- approved terms (abbreviation → full term) and role names that appear in the block's passages,
  so the drafter keeps one spelling of each role and expands abbreviations the same way everywhere;
- the section map for the references in the block: ``{{ref:SRC-7-U001}}`` → "ASSOCIATED DOCUMENTS",
  so a sentence can say where the reference points without writing a number.

The rendered text stays under a token cap; the least useful lines (terms, then roles) are dropped first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.schemas.v2 import ProtectedFacts, SectionPlan, SourceDocument, TemplateModel

from ..planning.budget import estimate_tokens
from ..planning.section_validator import covered_units

DEFAULT_MEMORY_TOKENS = 600


@dataclass
class DraftMemory:
    terms: dict[str, str] = field(default_factory=dict)       # abbreviation → full term
    roles: list[str] = field(default_factory=list)
    ref_targets: dict[str, str] = field(default_factory=dict)  # source section/unit id → "KEY heading"

    def render(self, max_tokens: int = DEFAULT_MEMORY_TOKENS) -> str:
        parts = {
            "refs": [f"{{{{ref:{k}}}}} points to {v}" for k, v in self.ref_targets.items()],
            "roles": [", ".join(self.roles)] if self.roles else [],
            "terms": [f"{a} = {f}" for a, f in self.terms.items()],
        }
        for drop in ("terms", "roles", "refs"):
            text = _render(parts)
            if estimate_tokens(text) <= max_tokens:
                return text
            while parts[drop] and estimate_tokens(_render(parts)) > max_tokens:
                parts[drop].pop()
        return _render(parts)


def _render(parts: dict[str, list[str]]) -> str:
    out = []
    if parts["roles"]:
        out.append("ROLE NAMES (keep exactly): " + parts["roles"][0])
    if parts["terms"]:
        out.append("ABBREVIATIONS: " + "; ".join(parts["terms"]))
    if parts["refs"]:
        out.append("REFERENCE TOKENS:\n" + "\n".join(parts["refs"]))
    return "\n".join(out)


def target_of(section_plan: SectionPlan, source: SourceDocument, template: TemplateModel) -> dict[str, str]:
    """Source section and unit IDs → the template section they were mapped to ("KEY heading")."""
    names = {t.section_id: f"{t.key or t.section_id} ({t.heading})" for t in template.sections}
    units = {s.section_id: [u.unit_id for u in s.units] for s in source.sections}
    out: dict[str, str] = {}
    for m in section_plan.mappings:
        if not m.target_section_id:
            continue
        for unit_id in covered_units(m, units):
            out[unit_id] = names.get(m.target_section_id, m.target_section_id)
        for section_id in m.source_section_ids:
            out.setdefault(section_id, names.get(m.target_section_id, m.target_section_id))
    return out


def build_memory(texts: Iterable[str], facts: ProtectedFacts, section_map: dict[str, str], ref_ids: Iterable[str]) -> DraftMemory:
    joined = " ".join(texts)
    terms = {a: f for f, a in facts.approved_terms.items() if re.search(rf"\b{re.escape(a)}\b", joined)}
    roles = [r for r in facts.roles if re.search(rf"\b{re.escape(r)}\b", joined, re.I)]
    refs = {r: section_map.get(r, "another part of this document") for r in dict.fromkeys(ref_ids)}
    return DraftMemory(terms=terms, roles=roles, ref_targets=refs)

"""Deterministic section-matching signals (zero tokens): heading names and section content.

Name: the source heading against each target's heading, key and aliases
(built-in defaults, plus ``TargetSection.aliases`` from the template config).

Content: every unit gets content labels (purpose, scope, definition,
procedure, restriction...) from its type, table headers, keywords and the
Phase 6 extractors; a section's profile is the share of each label. A
profile matches a target through the ``content_type``s of the target's
slots. A light bag-of-words cosine against the target's instructions adds
a little lexical evidence.

A list item that follows an intro line ending in ':' inherits the intro's
labels: "The following processes must not be automated:" makes the bullets
below it restrictions.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.v2 import ContentType, Modality, SourceDocument, SourceSection, SourceUnit, TargetSection, TemplateModel, UnitType

from ..quality.facts import _ROLE_COLUMN, extract_values, sentence_modalities, sentences

# ── Names ─────────────────────────────────────────────────────────────

DEFAULT_ALIASES: dict[str, list[str]] = {
    "PURPOSE": ["purpose", "objective", "objectives", "aim", "aims", "intention"],
    "APPLICABILITY": ["applicability", "scope", "area of application", "field of application", "scope of application", "validity"],
    "DEFINITIONS": ["definitions", "abbreviations", "definitions and abbreviations", "glossary", "terms and definitions",
                    "terminology", "acronyms"],
    "IMPLEMENTATION": ["implementation", "prerequisites", "pre requisites", "implementation and or pre requisites",
                       "implementation and prerequisites"],
    "ROLES": ["roles", "responsibilities", "roles and responsibilities", "raci", "accountabilities"],
    "PROCESS": ["process", "procedure", "procedures", "procedure process", "process description", "description",
                "instructions", "method", "methods", "steps"],
    "ASSOCIATED_DOCUMENTS": ["associated documents", "associated reference documents", "attachments", "appendices",
                             "appendix", "annex", "annexes", "forms", "templates"],
    "REFERENCES": ["references", "reference documents", "related documents", "bibliography", "literature"],
    "DOCUMENT_HISTORY": ["document history", "revision history", "change history", "version history", "history"],
    "DISTRIBUTION_OF_CONTROLLED_PRINTS": ["distribution", "controlled prints", "controlled copies", "distribution of controlled prints"],
}
_STOP = {"and", "or", "of", "the", "for", "a", "an", "to", "in", "on", "with", "by", "is", "are", "be", "this", "that", "as",
         "at", "it", "its", "if", "not", "no", "all", "any", "each", "from", "which", "who", "will", "can", "may", "must", "shall",
         "should", "have", "has", "e", "g", "eg", "i", "ie", "etc", "use", "please", "insert", "none", "add"}


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    words = re.findall(r"[a-z]+", (text or "").lower().replace("-", " "))
    return [_stem(w) for w in words if keep_stop or w not in _STOP]


def heading_tokens(heading: str) -> list[str]:
    heading = re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", heading or "")
    return tokens(heading)


def aliases_for(target: TargetSection) -> list[str]:
    return list(dict.fromkeys(
        [target.heading, (target.key or "").replace("_", " ")] + DEFAULT_ALIASES.get(target.key or "", []) + list(target.aliases)
    ))


def name_score(heading: str, target: TargetSection) -> tuple[float, Optional[str]]:
    """1.0 for an exact (normalized) match, high when an alias is fully contained, low for partial overlap."""
    head = heading_tokens(heading)
    if not head:
        return 0.0, None
    head_set = set(head)
    best, best_alias = 0.0, None
    for alias in aliases_for(target):
        alias_tokens = tokens(alias)
        if not alias_tokens:
            continue
        a = set(alias_tokens)
        if head == alias_tokens or head_set == a:
            return 1.0, alias
        common = len(head_set & a)
        if not common:
            continue
        alias_cov, head_cov = common / len(a), common / len(head_set)
        score = 0.55 + 0.4 * head_cov if alias_cov == 1 else 0.4 * (common / len(head_set | a))
        if score > best:
            best, best_alias = score, alias
    return round(best, 3), best_alias


# ── Content labels ────────────────────────────────────────────────────

_DOC_WORD = r"(?:sop|document|guidance|guideline|procedure|directive|work\s+instruction|standard|policy)"
_PURPOSE = re.compile(
    rf"\b(?:this|the\s+present)\s+{_DOC_WORD}\s+(?:also\s+|further\s+|therefore\s+)?"
    r"(?:describes|documents|defines|outlines|specifies|sets\s+out|establishes|provides|aims|is\s+intended|serves"
    r"|regulates|covers|explains|governs|details|summari[sz]es)"
    rf"|^\s*this\s+{_DOC_WORD}\s*:\s*$"
    r"|\bthis\s+is\s+the\s+(?:defined\s+)?(?:process|procedure)\b|\bbuilding\s+the\s+framework\b"
    r"|\bpurpose\b|\bobjective\b|\baims?\s+to\b|\bintention\b|\bit\s+describes\b",
    re.I,
)
_SCOPE = re.compile(
    r"\b(?:applicab\w*|applies|apply\s+to|in\s+scope|out\s+of\s+scope|not\s+in\s+scope|scope|binding\s+for|world-?wide"
    r"|globally|employees|contractors|sites?|divisions?|business\s+units?|departments?|geograph\w*|regions?|countries"
    r"|is\s+valid\s+for|does\s+not\s+apply|not\s+covered)\b",
    re.I,
)
_PREREQ = re.compile(
    r"\b(?:pre-?requisites?|prior\s+to|before\s+(?:starting|using|the\s+start)|effective\s+date|implement\w*"
    r"|transition\s+(?:period|plan)|curricul\w+|be\s+in\s+place|periodic\s+review)\b",
    re.I,
)
_RESP = re.compile(r"\b(?:responsible\s+(?:for|to)|accountab\w+|responsibilit\w+|raci|in\s+charge\s+of)\b", re.I)
_STEP = re.compile(r"\bsteps?\s*\d|\bstep\b", re.I)
_DEF_HEADER = re.compile(r"\b(?:terms?|abbreviations?|acronyms?|definitions?|expressions?)\b", re.I)
_REF_HEADER = re.compile(r"^(?:no\.?|nr\.?|#|name|title|document|reference|id)$", re.I)
_RECORD_HEADER = re.compile(r"\b(?:version|description\s+of\s+changes?|revision|author|change\s+history)\b", re.I)

# Which target content types each label supports, and how strongly.
LABEL_MATCH: dict[str, dict[str, float]] = {
    "purpose": {"purpose": 1.0},
    "scope": {"scope": 1.0},
    "definition": {"definition": 1.0},
    "prerequisite": {"prerequisite": 1.0},
    "responsibility": {"responsibility": 1.0, "ordered_procedure": 0.2},
    "ordered_procedure": {"ordered_procedure": 1.0, "decision": 0.8, "timing": 0.6, "process_input": 0.6,
                          "expected_output": 0.6, "frequency": 0.5, "duration": 0.5},
    "restriction": {"restriction": 1.0, "warning": 0.9, "ordered_procedure": 0.8, "decision": 0.6},
    "reference": {"reference": 1.0},
    "record": {"record": 1.0},
    "supporting_information": {"supporting_information": 0.3, "ordered_procedure": 0.15},
}


def own_document_ids(source: SourceDocument) -> set[str]:
    """This document's own IDs, from its cover page and history (boilerplate): e.g. '028-BIS-00535'."""
    ids: set[str] = set()
    for unit in source.iter_units():
        if unit.is_boilerplate:
            ids |= {v.normalized for v in extract_values(unit.text) if v.kind.value == "reference"}
    return ids


def unit_labels(unit: SourceUnit, own_ids: frozenset[str] = frozenset()) -> dict[str, float]:
    """Content labels of one unit, weights summing to 1.

    A reference to one of this document's own sub-documents (its number plus a suffix, e.g.
    '028-BIS-00535-RD00', 'BI-VQD-10505-S-AD02') is an ``associated_document``, not a general reference.
    """
    text = unit.text or ""
    raw: Counter = Counter()
    headers = unit.table_ref.header_cells if unit.table_ref else []
    first_header = headers[0] if headers else ""
    refs = [v for v in extract_values(text) if v.kind.value == "reference"]
    own = [r for r in refs if any(r.normalized != i and r.normalized.startswith(i) for i in own_ids)]
    ref_label = "associated_document" if own else "reference"

    if unit.unit_type == UnitType.TABLE_ROW and headers:
        if _DEF_HEADER.search(first_header):
            raw["definition"] += 2
        if _ROLE_COLUMN.search(first_header) or any(re.search(r"\braci\b|responsib", h, re.I) for h in headers):
            raw["responsibility"] += 2
        if _REF_HEADER.match(first_header.strip()) and any(_REF_HEADER.match(h.strip()) for h in headers[1:]):
            raw[ref_label] += 2
        if sum(bool(_RECORD_HEADER.search(h)) for h in headers) >= 2:
            raw["record"] += 2
    if unit.unit_type in (UnitType.REFERENCE,):
        raw[ref_label] += 2
    if unit.unit_type == UnitType.DEFINITION:
        raw["definition"] += 2
    if unit.unit_type == UnitType.PROCEDURE_STEP:
        raw["ordered_procedure"] += 1.5
    if unit.unit_type == UnitType.WARNING:
        raw["restriction"] += 1

    if own:
        raw["associated_document"] += 2
    elif refs:
        raw["reference"] += 1.5 if len(text) < 200 else 0.4

    if _PURPOSE.search(text):
        raw["purpose"] += 1
    scope_hits = len(_SCOPE.findall(text))
    if scope_hits:
        raw["scope"] += min(1.5, 0.6 * scope_hits)
    if _PREREQ.search(text):
        raw["prerequisite"] += 0.8
    if _RESP.search(text):
        raw["responsibility"] += 0.8
    if _STEP.search(text):
        raw["ordered_procedure"] += 0.5
    for sentence in sentences(text)[:6]:
        mods = sentence_modalities(sentence)
        if Modality.PROHIBITION in mods:
            raw["restriction"] += 1
        elif Modality.MANDATORY in mods:
            raw["ordered_procedure"] += 0.4
    if not raw:
        raw["supporting_information"] = 1
    total = sum(raw.values())
    return {label: weight / total for label, weight in raw.items()}


def labelled_units(
    units: list[SourceUnit], own_ids: frozenset[str] = frozenset()
) -> list[tuple[SourceUnit, dict[str, float]]]:
    """Unit labels, with list items inheriting the labels of an intro line ending in ':'."""
    out = []
    intro: Optional[dict[str, float]] = None
    for unit in units:
        labels = unit_labels(unit, own_ids)
        if unit.unit_type in (UnitType.BULLET, UnitType.PROCEDURE_STEP) and intro is not None:
            merged = Counter(intro)
            for label, weight in labels.items():
                if label != "supporting_information":
                    merged[label] += weight
            total = sum(merged.values())
            labels = {k: v / total for k, v in merged.items()}
        elif unit.unit_type not in (UnitType.BULLET, UnitType.PROCEDURE_STEP):
            intro = labels if unit.text.rstrip().endswith(":") and "supporting_information" not in labels else None
        out.append((unit, labels))
    return out


def profile(units: list[SourceUnit], own_ids: frozenset[str] = frozenset()) -> dict[str, float]:
    """Share of each label over the units (non-boilerplate)."""
    units = [u for u in units if not u.is_boilerplate and u.text.strip()]
    if not units:
        return {}
    acc: Counter = Counter()
    for _, labels in labelled_units(units, own_ids):
        acc.update(labels)
    return {label: round(weight / len(units), 3) for label, weight in acc.most_common()}


def target_types(target: TargetSection) -> set[str]:
    return {slot.content_type.value for slot in target.slots} or {ContentType.SUPPORTING_INFORMATION.value}


_ASSOCIATED_TARGET = re.compile(r"associat|attachment|annex|appendi", re.I)


def label_score(prof: dict[str, float], target: TargetSection) -> float:
    types = target_types(target)
    score = 0.0
    for label, share in prof.items():
        if label == "associated_document":
            # Slot content types cannot tell "associated documents" from "references": the target's name does.
            named = _ASSOCIATED_TARGET.search(" ".join([target.heading] + aliases_for(target)))
            score += share * (1.0 if named else 0.4 if "reference" in types else 0.0)
        else:
            score += share * max((LABEL_MATCH.get(label, {}).get(t, 0.0) for t in types), default=0.0)
    return score


# ── Bag of words ──────────────────────────────────────────────────────


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[t] * b[t] for t in a.keys() & b.keys())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def target_text(target: TargetSection) -> str:
    parts = [target.heading, " ".join(aliases_for(target)), target.purpose_instruction or ""]
    parts += [slot.instruction for slot in target.slots]
    return " ".join(parts)


# ── Section signals ───────────────────────────────────────────────────


@dataclass
class SectionSignal:
    section_id: str
    heading_path: str
    n_units: int
    profile: dict[str, float]
    name: dict[str, float] = field(default_factory=dict)      # target section_id → name score
    name_alias: dict[str, str] = field(default_factory=dict)  # target section_id → matched alias
    content: dict[str, float] = field(default_factory=dict)   # target section_id → content score

    def best(self, scores: dict[str, float]) -> tuple[Optional[str], float, float]:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked or ranked[0][1] <= 0:
            return None, 0.0, 0.0
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        return ranked[0][0], ranked[0][1], ranked[0][1] - second

    def combined(self) -> dict[str, float]:
        """Name-dominant when the name is a strong match, content-dominant otherwise."""
        strong_name = max(self.name.values(), default=0.0) >= 0.8
        w_name = 0.7 if strong_name else 0.35
        return {t: round(w_name * self.name.get(t, 0.0) + (1 - w_name) * self.content.get(t, 0.0), 3)
                for t in set(self.name) | set(self.content)}

    def profile_text(self, top: int = 3) -> str:
        items = [(k, v) for k, v in self.profile.items() if v >= 0.05][:top]
        shown = {"supporting_information": "other"}  # the catch-all bucket, not a content type to act on
        return ", ".join(f"{shown.get(k, k)} {round(v * self.n_units, 1):g}" for k, v in items) or "empty"


class SignalBuilder:
    def __init__(self, source: SourceDocument, template: TemplateModel):
        self.source = source
        self.template = template
        self.by_id = {s.section_id: s for s in source.sections}
        self.children: dict[Optional[str], list[SourceSection]] = {}
        for s in source.sections:
            self.children.setdefault(s.parent_id, []).append(s)
        self._target_bags = {t.section_id: Counter(tokens(target_text(t))) for t in template.sections}
        self.own_ids = frozenset(own_document_ids(source))

    def subtree(self, section: SourceSection) -> list[SourceSection]:
        out = [section]
        for child in self.children.get(section.section_id, []):
            out += self.subtree(child)
        return out

    def subtree_units(self, section: SourceSection) -> list[SourceUnit]:
        return [u for s in self.subtree(section) for u in s.units]

    def heading_path(self, section: SourceSection) -> str:
        parts = []
        node: Optional[SourceSection] = section
        while node is not None:
            parts.append(re.sub(r"\s+", " ", node.heading).strip())
            node = self.by_id.get(node.parent_id) if node.parent_id else None
        return " > ".join(reversed(parts))

    def signal(self, section: SourceSection, units: Optional[Iterable[SourceUnit]] = None) -> SectionSignal:
        units = list(units) if units is not None else self.subtree_units(section)
        prof = profile(units, self.own_ids)
        text_bag = Counter(tokens(" ".join(u.text for u in units if not u.is_boilerplate)))
        sig = SectionSignal(
            section_id=section.section_id,
            heading_path=self.heading_path(section),
            n_units=len([u for u in units if not u.is_boilerplate and u.text.strip()]),
            profile=prof,
        )
        for target in self.template.sections:
            score, alias = name_score(section.heading, target)
            sig.name[target.section_id] = score
            if alias:
                sig.name_alias[target.section_id] = alias
            if prof:
                sig.content[target.section_id] = round(
                    0.75 * label_score(prof, target) + 0.25 * _cosine(text_bag, self._target_bags[target.section_id]), 3
                )
        return sig

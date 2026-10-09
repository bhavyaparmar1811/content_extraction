"""Deterministic preservation checks: did a draft keep the source's facts? (Phase 6)

Each check works per source unit, against all the claims that cite it, so a
unit split across slots, or an actor moved to the responsibility slot, is not
a false alarm. Claims are read with the same extractors as the source.

    compare_values      numbers, percentages, dates, durations, deadlines, frequencies  (PRES-001, PRES-002)
    compare_references  document, form and system IDs, e-mail addresses, URLs           (PRES-006)
    compare_modality    must / must not / should / may, including imperatives            (PRES-004)
    compare_roles       roles dropped from, or introduced into, the cited content        (PRES-003)
    compare_quotes      quoted document or system names without an ID                     (PRES-006)

They run whether or not the job has a GWP. Units no claim cites are left to the
coverage gate (Phase 10).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

from app.schemas.v2 import (
    Claim,
    ClaimKind,
    FactKind,
    Gate,
    IssueCategory,
    IssueSource,
    Modality,
    ProtectedFacts,
    SectionDraft,
    Severity,
    SourceDocument,
    ValidationIssue,
)

from .facts import extract_values, find_roles, protected_units, text_modalities
from .normalize import PreservationConfig

_TIME_KINDS = {FactKind.DATE, FactKind.DURATION, FactKind.FREQUENCY, FactKind.DEADLINE}
_MODAL_WORDS = {
    Modality.PROHIBITION: "must not",
    Modality.MANDATORY: "must",
    Modality.RECOMMENDED: "should",
    Modality.PERMITTED: "may",
}


@dataclass(frozen=True)
class PlacedClaim:
    claim: Claim
    target_section_id: Optional[str] = None
    slot_id: Optional[str] = None


def placed_claims(drafts: Iterable[SectionDraft]) -> list[PlacedClaim]:
    return [
        PlacedClaim(claim, draft.target_section_id, slot.slot_id)
        for draft in drafts
        for slot in draft.slots
        for claim in slot.claims
        if not claim.is_gap_marker and claim.kind != ClaimKind.HEADING  # headings are source headings, copied as is
    ]


def _by_unit(claims: list[PlacedClaim]) -> dict[str, list[PlacedClaim]]:
    index: dict[str, list[PlacedClaim]] = defaultdict(list)
    for pc in claims:
        for unit_id in dict.fromkeys(pc.claim.source_unit_ids):
            index[unit_id].append(pc)
    return index


def _issue(
    check: str,
    key: str,
    severity: Severity,
    gate: Optional[Gate],
    message: str,
    unit_ids: list[str],
    claims: list[PlacedClaim],
) -> ValidationIssue:
    claim_ids = list(dict.fromkeys(pc.claim.claim_id for pc in claims))
    digest = hashlib.sha1("|".join([check, key, ",".join(unit_ids), ",".join(claim_ids)]).encode()).hexdigest()[:10]
    sections = {pc.target_section_id for pc in claims}
    slots = {pc.slot_id for pc in claims}
    return ValidationIssue(
        issue_id=f"ISS-{digest}",
        severity=severity,
        category=IssueCategory.PRESERVATION,
        source=IssueSource.DETERMINISTIC,
        gate=gate,
        target_section_id=sections.pop() if len(sections) == 1 else None,
        slot_id=slots.pop() if len(slots) == 1 else None,
        claim_ids=claim_ids,
        unit_ids=unit_ids,
        message=message,
    )


def _rule(kind: FactKind) -> str:
    return "PRES-002" if kind in _TIME_KINDS else "PRES-001"


# ── Values and references ─────────────────────────────────────────────


def _source_values(facts: ProtectedFacts, references: bool) -> dict[str, dict[str, tuple[FactKind, str]]]:
    out: dict[str, dict[str, tuple[FactKind, str]]] = defaultdict(dict)
    for v in facts.values:
        if (v.kind == FactKind.REFERENCE) == references:
            out[v.unit_id].setdefault(v.normalized, (v.kind, v.raw))
    return out


def _claim_values(claims: list[PlacedClaim], config: PreservationConfig, references: bool) -> dict[str, dict[str, tuple[FactKind, str]]]:
    out: dict[str, dict[str, tuple[FactKind, str]]] = {}
    for pc in claims:
        values: dict[str, tuple[FactKind, str]] = {}
        for v in extract_values(pc.claim.text, config):
            if (v.kind == FactKind.REFERENCE) == references:
                values.setdefault(v.normalized, (v.kind, v.raw))
        out[pc.claim.claim_id] = values
    return out


def _compare(
    facts: ProtectedFacts,
    doc: SourceDocument,
    claims: list[PlacedClaim],
    config: PreservationConfig,
    references: bool,
) -> list[ValidationIssue]:
    source = _source_values(facts, references)  # what must be kept: the (curatable) registry
    drafted = _claim_values(claims, config, references)
    index = _by_unit(claims)
    texts = {u.unit_id: u.text for u in doc.iter_units()}
    present_in_text: dict[str, set[str]] = {}
    issues: list[ValidationIssue] = []

    def in_text(unit_id: str) -> set[str]:
        if unit_id not in present_in_text:
            present_in_text[unit_id] = {
                v.normalized for v in extract_values(texts.get(unit_id, ""), config)
                if (v.kind == FactKind.REFERENCE) == references
            }
        return present_in_text[unit_id]

    # Values a claim states that none of its cited units says (anything in the unit text may be repeated).
    added: dict[str, dict[str, tuple[FactKind, str]]] = {}
    for pc in claims:
        allowed: set[str] = set()
        for unit_id in pc.claim.source_unit_ids:
            allowed |= set(source.get(unit_id, {})) | in_text(unit_id)
        extra = {n: kv for n, kv in drafted[pc.claim.claim_id].items() if n not in allowed}
        if extra:
            added[pc.claim.claim_id] = extra

    for unit_id, unit_values in source.items():
        cited_by = index.get(unit_id)
        if not cited_by:
            continue
        present: set[str] = set()
        for pc in cited_by:
            present |= set(drafted[pc.claim.claim_id])
        for normalized, (kind, raw) in unit_values.items():
            if normalized in present:
                continue
            # A changed value: an added value of the same kind in a claim citing this unit.
            swap = next(
                ((pc, n, kv) for pc in cited_by for n, kv in added.get(pc.claim.claim_id, {}).items()
                 if kv[0] == kind or (references and kv[0] == FactKind.REFERENCE)),
                None,
            )
            if references:
                rule, severity, gate = "PRES-006", Severity.HIGH, Gate.HIGH_RISK_UNRESOLVED
            else:
                rule, severity, gate = _rule(kind), Severity.CRITICAL, Gate.NUMERICAL_CHANGE
            if swap:
                pc, new_norm, (_, new_raw) = swap
                del added[pc.claim.claim_id][new_norm]
                issues.append(_issue(
                    "value_changed", f"{normalized}->{new_norm}", severity, gate,
                    f"[{rule}] {kind.value} changed: source '{raw}' ({unit_id}) became '{new_raw}' in {pc.claim.claim_id}.",
                    [unit_id], [pc],
                ))
            else:
                issues.append(_issue(
                    "value_missing", normalized, severity, gate,
                    f"[{rule}] {kind.value} '{raw}' from {unit_id} is missing from the claims that cite it.",
                    [unit_id], cited_by,
                ))

    for pc in claims:
        for normalized, (kind, raw) in added.get(pc.claim.claim_id, {}).items():
            if references:
                rule, gate = "PRES-006", Gate.UNSUPPORTED_CLAIM
            else:
                rule, gate = _rule(kind), Gate.NUMERICAL_CHANGE
            issues.append(_issue(
                "value_added", normalized, Severity.HIGH, gate,
                f"[{rule}] {pc.claim.claim_id} states {kind.value} '{raw}', which its cited units "
                f"({', '.join(pc.claim.source_unit_ids)}) do not contain.",
                list(pc.claim.source_unit_ids), [pc],
            ))
    return issues


def compare_values(
    facts: ProtectedFacts, source: SourceDocument, claims: list[PlacedClaim], config: Optional[PreservationConfig] = None
) -> list[ValidationIssue]:
    return _compare(facts, source, claims, config or PreservationConfig(), references=False)


def compare_references(
    facts: ProtectedFacts, source: SourceDocument, claims: list[PlacedClaim], config: Optional[PreservationConfig] = None
) -> list[ValidationIssue]:
    return _compare(facts, source, claims, config or PreservationConfig(), references=True)


# ── Modality ──────────────────────────────────────────────────────────


def _words(modalities: set[Modality]) -> str:
    return ", ".join(_MODAL_WORDS[m] for m in sorted(modalities, key=lambda m: list(_MODAL_WORDS).index(m))) or "none"


def compare_modality(facts: ProtectedFacts, claims: list[PlacedClaim],
                     source_doc: Optional[SourceDocument] = None) -> list[ValidationIssue]:
    """Obligation strength per unit. A plain statement made explicit ('submits' → 'must submit' or an
    imperative) is allowed but listed for review (medium, no gate); anything that removes, weakens or
    strengthens a stated modality is not."""
    source: dict[str, set[Modality]] = defaultdict(set)
    for o in facts.obligations:
        if o.modality in _MODAL_WORDS:
            source[o.unit_id].add(o.modality)
    plain = set()  # protected passages whose own text states no obligation at all
    if source_doc is not None:
        plain = {u.unit_id for u in protected_units(source_doc) if not (text_modalities([u.text]) & set(_MODAL_WORDS))}
    issues = []
    for unit_id, cited_by in _by_unit(claims).items():
        s = source.get(unit_id, set())
        c = text_modalities(pc.claim.text for pc in cited_by) & set(_MODAL_WORDS)
        lost, added = s - c, c - s
        if not lost and added == {Modality.MANDATORY} and not s and unit_id in plain:
            # Allowed (the GWP asks for "must" on mandatory steps), but a reviewer sees each one.
            issues.append(_issue(
                "made_mandatory", unit_id, Severity.MEDIUM, None,
                f"[PRES-004] {unit_id} states no obligation; the draft makes it mandatory ('must' or an instruction). "
                "Check that the source means it as a requirement.",
                [unit_id], cited_by,
            ))
            continue
        if not lost and not (added - {Modality.MANDATORY}):
            continue
        severe = bool(lost & {Modality.PROHIBITION, Modality.MANDATORY})
        if lost:
            what = f"source states '{_words(s)}', the draft states '{_words(c)}'"
        else:
            what = f"the draft adds '{_words(added - {Modality.MANDATORY})}' that the source does not state"
        issues.append(_issue(
            "modality", f"{sorted(m.value for m in s)}->{sorted(m.value for m in c)}",
            Severity.CRITICAL if severe else Severity.HIGH, Gate.HIGH_RISK_UNRESOLVED,
            f"[PRES-004] Obligation strength changed for {unit_id}: {what}.",
            [unit_id], cited_by,
        ))
    return issues


# ── Roles ─────────────────────────────────────────────────────────────


def _canonical_role(role: str, expansions: dict[str, str]) -> str:
    words = [expansions.get(w, w) for w in role.split()]
    text = " ".join(words).lower()
    text = re.sub(r"^(?:the|a|an)\s+", "", text)
    return re.sub(r"s$", "", text.strip())


def compare_roles(facts: ProtectedFacts, source: SourceDocument, claims: list[PlacedClaim]) -> list[ValidationIssue]:
    expansions = {abbr: full for full, abbr in facts.approved_terms.items()}
    known = list(facts.roles) + list(facts.approved_terms) + list(facts.approved_terms.values())
    known_roles = set(facts.roles)

    def roles_in(text: str) -> dict[str, str]:
        out = {}
        for role in find_roles(text, known):
            canonical = _canonical_role(role, expansions)
            is_role = role in known_roles or role not in facts.approved_terms and role not in expansions
            if is_role and canonical:
                out.setdefault(canonical, role)
        return out

    units = {u.unit_id: u for u in source.iter_units()}
    unit_roles = {uid: roles_in(u.text) for uid, u in units.items()}
    issues = []
    for unit_id, cited_by in _by_unit(claims).items():
        if unit_id not in unit_roles:
            continue
        drafted: dict[str, str] = {}
        for pc in cited_by:
            drafted.update(roles_in(pc.claim.text))
        for canonical, role in unit_roles[unit_id].items():
            if canonical not in drafted:
                issues.append(_issue(
                    "role_missing", canonical, Severity.HIGH, Gate.HIGH_RISK_UNRESOLVED,
                    f"[PRES-003] Role '{role}' in {unit_id} is not named in the claims that cite it.",
                    [unit_id], cited_by,
                ))
    for pc in claims:
        allowed: set[str] = set()
        for unit_id in pc.claim.source_unit_ids:
            allowed |= set(unit_roles.get(unit_id, {}))
        for canonical, role in roles_in(pc.claim.text).items():
            if canonical not in allowed:
                issues.append(_issue(
                    "role_added", canonical, Severity.HIGH, Gate.UNSUPPORTED_CLAIM,
                    f"[PRES-003] {pc.claim.claim_id} names role '{role}', which its cited units do not mention.",
                    list(pc.claim.source_unit_ids), [pc],
                ))
    return issues


# ── Quoted names ──────────────────────────────────────────────────────

# A quoted title or name: "Refer to “UiPath Development Guideline”." The replacement character stands for quotes the
# extraction could not decode. The name must start with a capital and close before a non-letter, so "RPAS’s" is no quote.
_QUOTED = re.compile(r"[“\"„«�]([A-Z0-9][^“”\"„«»�]{2,80}?)[”\"»�](?![A-Za-z])")


def quoted_names(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTED.finditer(text or "") if len(m.group(1).split()) >= 2]


def compare_quotes(source: SourceDocument, claims: list[PlacedClaim]) -> list[ValidationIssue]:
    """PRES-006 for names without an ID: a quoted document or system title in the source is kept, word for word."""
    units = {u.unit_id: u for u in source.iter_units()}
    issues = []
    for unit_id, cited_by in _by_unit(claims).items():
        if unit_id not in units:
            continue
        drafted = re.sub(r"\s+", " ", " ".join(pc.claim.text for pc in cited_by)).lower()
        for name in quoted_names(units[unit_id].text):
            if re.sub(r"\s+", " ", name).lower() not in drafted:
                issues.append(_issue(
                    "quoted", name, Severity.HIGH, Gate.HIGH_RISK_UNRESOLVED,
                    f"[PRES-006] quoted name '{name}' from {unit_id} is missing from the claims that cite it.",
                    [unit_id], cited_by,
                ))
    return issues


# ── All checks ────────────────────────────────────────────────────────


def check_preservation(
    facts: ProtectedFacts,
    source: SourceDocument,
    drafts: Iterable[SectionDraft],
    config: Optional[PreservationConfig] = None,
) -> list[ValidationIssue]:
    claims = placed_claims(drafts)
    return (
        compare_values(facts, source, claims, config)
        + compare_references(facts, source, claims, config)
        + compare_modality(facts, claims, source)
        + compare_roles(facts, source, claims)
        + compare_quotes(source, claims)
    )

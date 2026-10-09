"""Reconciliation (Phase 12): checks over the whole assembled document, after every section passed on its own.

Deterministic (``reconcile_checks``):
- abbreviations: each one used in the prose is in the abbreviations table or spelled out ("Full Term (FT)"), before
  its first use; one abbreviation has one meaning (low; a conflicting table entry is medium);
- role names: a role named in the prose is in the roles table (low);
- numbering: no two chapters or headings share a number (high, ``sequence_violation``);
- cross-references: the resolver's issues (``assembly.xref_resolver``) are added by the stage.

LLM (``reconcile``, narrow): one call reads all prose claims and reports contradictions, duplicates and terms named
two ways across sections. It may patch a reworded claim (never one copied verbatim); a patch is applied only if the
whole document still passes validation without a new blocking issue on that claim, its reference tokens are the
same, and it changes nothing else. Applied patches are a new draft version with ``origin: reconcile`` and a low note;
everything else is an advisory issue for the reviewer.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.schemas.v2 import (
    ClaimKind,
    DraftOrigin,
    Gate,
    IssueCategory,
    IssueSource,
    NumberKind,
    NumberMap,
    SectionDraft,
    Severity,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)
from app.services.llm.prompts.v2.reconcile import PROMPT_VERSION, SYSTEM_PROMPT, USER_PROMPT

from ..drafting.drafter import _call
from ..drafting.refs import tokens_of
from .critic import Critic
from .facts import extract_terms, find_roles

RECONCILE_TAG = "[reconcile"
_PROSE = (ClaimKind.PARAGRAPH, ClaimKind.BULLET, ClaimKind.STEP)
_ABBR = re.compile(r"(?<![\w/-])([A-Z][A-Z0-9&]{1,7})(s?)(?![\w/-])")  # all capitals ("SOP", "SOPs"); not "UiPath"
_NOT_ABBR = {"I", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "OK", "NA", "AND", "OR", "NOT", "NO", "YES",
             "THE", "FOR", "ALL", "NEW", "TBD"}
_LIST_LIMIT = 12  # names shown in one summary issue
_ABBR_HEADER = re.compile(r"abbrev|acronym", re.I)
_TERM_HEADER = re.compile(r"^\s*terms?\b", re.I)
_ROLE_HEADER = re.compile(r"\brole", re.I)


def _issue(key: str, severity: Severity, category: IssueCategory, message: str, gate: Optional[Gate] = None,
           target: Optional[str] = None, claim_ids: Optional[list[str]] = None, unit_ids: Optional[list[str]] = None,
           source: IssueSource = IssueSource.DETERMINISTIC) -> ValidationIssue:
    digest = hashlib.sha1(f"reconcile|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(issue_id=f"ISS-{digest}", severity=severity, category=category, source=source, gate=gate,
                           target_section_id=target, claim_ids=claim_ids or [], unit_ids=unit_ids or [], message=message)


def _prose(drafts: list[SectionDraft]):
    for draft in drafts:
        for claim in draft.iter_claims():
            if claim.kind in _PROSE and not claim.is_gap_marker:
                yield draft, claim


def _table_cells(drafts: list[SectionDraft], source: SourceDocument, header: re.Pattern) -> list[tuple[str, list[str]]]:
    """(claim_id, cells) of drafted table rows whose table's first header matches *header*."""
    units = {u.unit_id: u for u in source.iter_units()}
    out = []
    for draft in drafts:
        for claim in draft.iter_claims():
            unit = units.get(claim.source_unit_ids[0]) if claim.source_unit_ids else None
            if (claim.kind == ClaimKind.TABLE_ROW and unit is not None and unit.table_ref is not None
                    and unit.table_ref.header_cells and header.search(unit.table_ref.header_cells[0] or "")):
                out.append((claim.claim_id, [c.strip() for c in claim.text.split("|")]))
    return out


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _same_meaning(a: str, b: str) -> bool:
    """Loosely the same expansion: case, plurals and possessives aside, one contains the other ("BI's Lean
    Validation Approach" and "Lean Validation Approach")."""
    def stems(text: str) -> list[str]:
        return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _words(re.sub(r"[\u2019']s\b", "", text)).split()]
    x, y = " ".join(stems(a)), " ".join(stems(b))
    return x == y or (bool(x) and bool(y) and (f" {x} " in f" {y} " or f" {y} " in f" {x} "))


def _listed(items: list[tuple[str, str]]) -> str:
    shown = ", ".join(f"{name} ({claim_id})" for name, claim_id in items[:_LIST_LIMIT])
    return shown + (f" and {len(items) - _LIST_LIMIT} more" if len(items) > _LIST_LIMIT else "")


def abbreviation_issues(drafts: list[SectionDraft], source: SourceDocument) -> list[ValidationIssue]:
    table: dict[str, list[tuple[str, str]]] = defaultdict(list)  # abbreviation → (expansion, claim_id)
    for claim_id, cells in _table_cells(drafts, source, _ABBR_HEADER):
        if len(cells) >= 2 and cells[0]:
            table[cells[0]].append((cells[1], claim_id))
    terms = {cells[0].lower() for _, cells in _table_cells(drafts, source, _TERM_HEADER) if cells and cells[0]}
    issues = []
    for abbr, rows in table.items():
        meanings = [e for e, _ in rows if e]
        if any(not _same_meaning(meanings[0], m) for m in meanings[1:]):
            issues.append(_issue(f"abbr-conflict|{abbr}", Severity.MEDIUM, IssueCategory.CONSISTENCY,
                                 f"{RECONCILE_TAG}:abbreviation] '{abbr}' has {len(meanings)} meanings in the abbreviations "
                                 f"table: {'; '.join(meanings)}.", claim_ids=[c for _, c in rows]))
    prose = list(_prose(drafts))
    spelled: dict[str, tuple[int, str, str]] = {}  # abbreviation → (position, claim_id, expansion) of its first spelling
    for i, (_, claim) in enumerate(prose):
        for expansion, abbr in extract_terms([claim.text]).items():
            spelled.setdefault(abbr, (i, claim.claim_id, expansion))
    first_use: dict[str, tuple[int, str]] = {}
    for i, (_, claim) in enumerate(prose):
        for m in _ABBR.finditer(re.sub(r"\{\{ref:[^}]*\}\}", " ", claim.text)):
            abbr = m.group(1)
            if abbr not in _NOT_ABBR and abbr.lower() not in terms:
                first_use.setdefault(abbr, (i, claim.claim_id))
    undefined = []
    for abbr, (i, claim_id) in sorted(first_use.items(), key=lambda kv: kv[1][0]):
        if abbr in table:
            spelled_as = spelled.get(abbr)
            if spelled_as and not any(_same_meaning(spelled_as[2], e) for e, _ in table[abbr]):
                issues.append(_issue(f"abbr-differs|{abbr}", Severity.LOW, IssueCategory.CONSISTENCY,
                                     f"{RECONCILE_TAG}:abbreviation] '{abbr}' is spelled out as '{spelled_as[2]}' in "
                                     f"{spelled_as[1]}, but the abbreviations table says '{table[abbr][0][0]}'.",
                                     claim_ids=[spelled_as[1]]))
        elif abbr in spelled and spelled[abbr][0] > i:
            issues.append(_issue(f"abbr-late|{abbr}", Severity.LOW, IssueCategory.CONSISTENCY,
                                 f"{RECONCILE_TAG}:abbreviation] '{abbr}' is first used in {claim_id} but spelled out "
                                 f"only later, in {spelled[abbr][1]}.", claim_ids=[claim_id, spelled[abbr][1]]))
        elif abbr not in spelled:
            undefined.append((abbr, claim_id))
    if undefined and table:  # without an abbreviations table there is nothing to hold the document to
        issues.append(_issue("abbr-undefined", Severity.LOW, IssueCategory.CONSISTENCY,
                             f"{RECONCILE_TAG}:abbreviation] {len(undefined)} abbreviation(s) are neither in the "
                             f"abbreviations table nor spelled out in the text: {_listed(undefined)}.",
                             claim_ids=[c for _, c in undefined][:_LIST_LIMIT]))
    return issues


def role_issues(drafts: list[SectionDraft], source: SourceDocument) -> list[ValidationIssue]:
    roles = {cells[0] for _, cells in _table_cells(drafts, source, _ROLE_HEADER) if cells and cells[0]}
    if not roles:
        return []
    known = {re.sub(r"\s+", " ", r).strip().lower() for r in roles}
    seen: dict[str, str] = {}
    for _, claim in _prose(drafts):
        for role in find_roles(claim.text, roles):
            key = re.sub(r"\s+", " ", role).strip().lower()
            if key not in known and not any(key in k or k in key for k in known):
                seen.setdefault(role, claim.claim_id)
    if not seen:
        return []
    return [_issue("roles", Severity.LOW, IssueCategory.CONSISTENCY,
                   f"{RECONCILE_TAG}:role] {len(seen)} role(s) named in the text are not in the roles table: "
                   f"{_listed(list(seen.items()))}.", claim_ids=list(seen.values())[:_LIST_LIMIT])]


def numbering_issues(number_map: NumberMap) -> list[ValidationIssue]:
    """Two chapters or headings with the same number (each bookmark is one heading)."""
    by_bookmark = {}
    for e in number_map.entries:
        if e.kind in (NumberKind.SECTION, NumberKind.HEADING) and e.exact and e.bookmark:
            by_bookmark.setdefault(e.bookmark, e.target_number)
    clashes = [n for n, k in Counter(by_bookmark.values()).items() if k > 1]
    return [_issue(f"number|{n}", Severity.HIGH, IssueCategory.ORDER,
                   f"{RECONCILE_TAG}:numbering] two headings get the number {n}.", Gate.SEQUENCE_VIOLATION)
            for n in clashes]


def reconcile_checks(drafts: list[SectionDraft], source: SourceDocument, number_map: NumberMap) -> list[ValidationIssue]:
    return abbreviation_issues(drafts, source) + role_issues(drafts, source) + numbering_issues(number_map)


# ── LLM pass ──────────────────────────────────────────────────────────


class ReconcilePatch(BaseModel):
    claim_id: str
    text: str = Field(description="The claim's new text")
    reason: str = Field(description="One short sentence")


class ReconcileFinding(BaseModel):
    problem: Literal["contradiction", "duplicate", "terminology"]
    claim_ids: list[str]
    note: str
    patch: Optional[ReconcilePatch] = None


class ReconcileOutput(BaseModel):
    findings: list[ReconcileFinding] = Field(default_factory=list)


_SEVERITY = {"contradiction": Severity.MEDIUM, "duplicate": Severity.LOW, "terminology": Severity.LOW}
_CATEGORY = {"contradiction": IssueCategory.SEMANTIC, "duplicate": IssueCategory.STRUCTURE,
             "terminology": IssueCategory.CONSISTENCY}


@dataclass
class ReconcileRun:
    issues: list[ValidationIssue] = field(default_factory=list)
    patched: dict[str, SectionDraft] = field(default_factory=dict)  # target_section_id → patched draft
    applied: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"calls": 0})
    skipped: Optional[str] = None


def reconcile_prompt(drafts: list[SectionDraft], template: TemplateModel, max_chars: int,
                     reworded: Callable[[Any], bool]) -> Optional[str]:
    headings = {s.section_id: s.heading for s in template.sections}
    for limit in (None, 300, 160):
        lines = []
        for draft in drafts:
            claims = [c for c in draft.iter_claims() if c.kind in _PROSE and not c.is_gap_marker]
            if not claims:
                continue
            lines.append(f"## {draft.target_section_id} {headings.get(draft.target_section_id, '')}")
            for c in claims:
                text = re.sub(r"\s+", " ", c.text).strip()
                if limit and len(text) > limit:
                    text = text[: limit - 1] + "…"
                lines.append(f"[{c.claim_id}] ({c.kind.value}, {'reworded' if reworded(c) else 'verbatim'}) {text}")
        body = "\n".join(lines)
        if len(body) <= max_chars:
            return USER_PROMPT.format(claims=body)
    return None


def apply_patch(drafts: list[SectionDraft], patch: ReconcilePatch, validate,
                reworded: Callable[[Any], bool]) -> tuple[Optional[SectionDraft], str]:
    """The patched section draft, or None with the reason it was refused.

    ``validate(drafts) -> list[ValidationIssue]`` runs the full deterministic validation on a whole set of drafts;
    ``reworded(claim)`` says whether the claim differs from its source wording (only those may be patched).
    """
    draft = next((d for d in drafts if any(c.claim_id == patch.claim_id for c in d.iter_claims())), None)
    if draft is None:
        return None, "unknown claim"
    claim = next(c for c in draft.iter_claims() if c.claim_id == patch.claim_id)
    text = re.sub(r"\s+", " ", patch.text or "").strip()
    if claim.kind not in _PROSE or claim.is_gap_marker:
        return None, f"{claim.kind.value} claims are never reworded"
    if not reworded(claim):
        return None, "the claim is the source's own wording (verbatim)"
    if not text or text == claim.text:
        return None, "no change"
    if sorted(tokens_of(text)) != sorted(tokens_of(claim.text)):
        return None, "the patch changes the claim's cross-reference tokens"
    patched = draft.model_copy(deep=True)
    for c in patched.iter_claims():
        if c.claim_id == patch.claim_id:
            c.text = text
    others = [d for d in drafts if d.target_section_id != draft.target_section_id]

    def blocking(issues: list[ValidationIssue]) -> set[str]:
        touched = set(claim.source_unit_ids)
        return {re.sub(r"C-[\w-]+", "C", i.message) for i in issues
                if (i.gate is not None or i.severity in (Severity.CRITICAL, Severity.HIGH))
                and (patch.claim_id in i.claim_ids or touched & set(i.unit_ids))}

    new = blocking(validate(others + [patched])) - blocking(validate(drafts))
    if new:
        return None, f"it fails validation: {sorted(new)[0][:200]}"
    return patched, "ok"


async def reconcile(drafts: list[SectionDraft], template: TemplateModel, source: SourceDocument, validate,
                    chain_factory: Any, rate_limiter: Any = None, max_chars: int = 60_000) -> ReconcileRun:
    """The LLM pass: findings become advisory issues; patches of reworded claims are applied if they re-validate."""
    run = ReconcileRun()
    if chain_factory is None:
        run.skipped = "no LLM configured"
        return run
    reworded = Critic(source, template).reworded  # differs from its source passage: not the source's own wording
    prompt = reconcile_prompt(drafts, template, max_chars, reworded)
    if prompt is None:
        run.skipped = f"the document's claims exceed {max_chars} characters"
        return run
    chain = chain_factory.create_structured_planner(ReconcileOutput, include_raw=True)
    try:
        output = await _call(chain, [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)],
                             ReconcileOutput, run.usage, rate_limiter, "reconcile")
    except Exception as exc:  # the deterministic checks still stand
        logger.warning(f"Reconciliation call failed: {exc}")
        run.skipped = f"the call failed: {str(exc)[:200]}"
        return run
    known = {c.claim_id: d.target_section_id for d in drafts for c in d.iter_claims()}
    current = list(drafts)
    for finding in output.findings:
        ids = [c for c in dict.fromkeys(finding.claim_ids) if c in known]
        if not ids:
            continue
        note = re.sub(r"\s+", " ", finding.note).strip()[:400]
        if finding.patch is not None and finding.patch.claim_id in known:
            patched, why = apply_patch(current, finding.patch, validate, reworded)
            record = {"claim_id": finding.patch.claim_id, "problem": finding.problem, "reason": finding.patch.reason[:300]}
            if patched is not None:
                current = [patched if d.target_section_id == patched.target_section_id else d for d in current]
                run.patched[patched.target_section_id] = patched
                run.applied.append(record)
                run.issues.append(_issue(
                    f"patched|{finding.patch.claim_id}|{finding.problem}", Severity.LOW, _CATEGORY[finding.problem],
                    f"{RECONCILE_TAG}:patched] {finding.patch.claim_id} reworded to fix a {finding.problem} with "
                    f"{', '.join(i for i in ids if i != finding.patch.claim_id) or 'another claim'}: "
                    f"{finding.patch.reason[:200]}", target=known[finding.patch.claim_id], claim_ids=ids,
                    source=IssueSource.CRITIC))
                continue
            run.rejected.append({**record, "refused": why})
            note += f" (patch refused: {why})"
        run.issues.append(_issue(
            f"{finding.problem}|{','.join(sorted(ids))}", _SEVERITY[finding.problem], _CATEGORY[finding.problem],
            f"{RECONCILE_TAG}:{finding.problem}] {', '.join(ids)}: {note}", target=known[ids[0]], claim_ids=ids,
            source=IssueSource.CRITIC))
    for draft_id, draft in run.patched.items():
        run.patched[draft_id] = draft.model_copy(update={"origin": DraftOrigin.RECONCILE})
    return run


__all__ = ["PROMPT_VERSION", "RECONCILE_TAG", "ReconcileOutput", "ReconcileRun", "apply_patch", "reconcile",
           "reconcile_checks", "reconcile_prompt"]

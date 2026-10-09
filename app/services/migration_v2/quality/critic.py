"""Semantic critic (Phase 10, migration_plan.md §17.2): one LLM call per section, advisory.

Only claims a GWP rewrite reworded are reviewed: a claim copied word for word from its passage cannot change
its meaning, so placement mode costs nothing. Each reworded claim is shown next to the source passages it
cites (with reference tokens), under its slot's question, with the job's PRES rules.

Findings become ``ValidationIssue``s with ``source: critic`` and no gate: the critic can never pass or fail a
hard gate by itself. A high finding about meaning sends the slot to targeted repair (``repair.py``); a
``source_conflict`` or ``wrong_slot`` finding goes to a reviewer, since rewording cannot fix it. Findings
that name claims the critic was not shown are dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.schemas.v2 import (
    Claim,
    ClaimKind,
    GwpRuleSet,
    IssueCategory,
    IssueSource,
    RuleCategory,
    SectionDraft,
    Severity,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)
from app.services.llm.prompts.v2.critic import PROMPT_VERSION, SYSTEM_PROMPT, USER_PROMPT

from ..drafting.drafter import _call
from ..drafting.refs import internal_refs, tokenize

DEFAULT_BLOCK_TOKENS = 3000
_CHARS_PER_TOKEN = 4

Problem = Literal["meaning_changed", "omission", "condition_lost", "unsupported_addition", "contradiction",
                  "source_conflict", "wrong_slot", "ambiguity", "redundancy"]

CATEGORY: dict[str, IssueCategory] = {
    "meaning_changed": IssueCategory.PRESERVATION,
    "omission": IssueCategory.PRESERVATION,
    "condition_lost": IssueCategory.PRESERVATION,
    "unsupported_addition": IssueCategory.TRACEABILITY,
    "contradiction": IssueCategory.SEMANTIC,
    "source_conflict": IssueCategory.CONSISTENCY,
    "wrong_slot": IssueCategory.STRUCTURE,
    "ambiguity": IssueCategory.SEMANTIC,
    "redundancy": IssueCategory.SEMANTIC,
}
# Rewording cannot fix these: a reviewer decides.
NOT_REPAIRABLE = frozenset({"source_conflict", "wrong_slot"})
# Never above medium, so they never trigger a repair on their own.
_SOFT = frozenset({"ambiguity", "redundancy"})
_SEVERITY = {"high": Severity.HIGH, "medium": Severity.MEDIUM, "low": Severity.LOW}


class CriticFinding(BaseModel):
    claim_ids: list[str] = Field(description="Claim IDs exactly as given")
    problem: Problem
    severity: Literal["high", "medium", "low"]
    explanation: str


class CriticOutput(BaseModel):
    findings: list[CriticFinding] = Field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def critic_problem(issue: ValidationIssue) -> Optional[str]:
    """The critic's problem kind of an issue, read back from its message prefix."""
    m = re.match(r"\[critic:(\w+)\]", issue.message)
    return m.group(1) if m and issue.source == IssueSource.CRITIC else None


@dataclass
class Entry:
    slot_ref: str
    slot_header: str
    claim: Claim
    text: str  # rendered claim with its sources


@dataclass
class CriticRun:
    issues: list[ValidationIssue] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"calls": 0})
    reviewed: list[str] = field(default_factory=list)   # sections the critic read
    failed: list[str] = field(default_factory=list)     # sections whose call failed twice


class Critic:
    def __init__(self, source: SourceDocument, template: TemplateModel, rules: Optional[GwpRuleSet] = None,
                 block_tokens: int = DEFAULT_BLOCK_TOKENS):
        self.source = source
        self.template = template
        self.block_tokens = block_tokens
        refs = internal_refs(source)
        self.unit_text = {u.unit_id: tokenize(u, refs.get(u.unit_id, [])) for u in source.iter_units()}
        self.sections = {s.section_id: s for s in template.sections}
        self.slots = {s.slot_id: s for s in template.iter_slots()}
        self.rules = [r for r in (rules.rules if rules else []) if r.category == RuleCategory.PRESERVATION]

    def reworded(self, claim: Claim) -> bool:
        """True unless the claim is its single passage, or a verbatim part of it."""
        if claim.is_gap_marker or claim.kind not in (ClaimKind.PARAGRAPH, ClaimKind.BULLET, ClaimKind.STEP):
            return False
        units = [u for u in claim.source_unit_ids if u in self.unit_text]
        if len(units) != 1:
            return bool(units)
        return _norm(claim.text) not in _norm(self.unit_text[units[0]])

    def entries(self, draft: SectionDraft, skip: frozenset[str] = frozenset()) -> list[Entry]:
        """The reworded claims of ``draft`` to review, minus the claim IDs in ``skip`` (already reviewed)."""
        target = self.sections.get(draft.target_section_id)
        out = []
        for slot in draft.slots:
            tslot = self.slots.get(slot.slot_id)
            ref = f"{(target.key if target else None) or draft.target_section_id}.{(tslot.key if tslot else None) or slot.slot_id}"
            instruction = re.sub(r"\s+", " ", tslot.instruction if tslot else "")[:200]
            header = f"### {ref} | {tslot.content_type.value if tslot else '?'} | {instruction}"
            for claim in slot.claims:
                if claim.claim_id in skip or not self.reworded(claim):
                    continue
                sources = "\n".join(f"    source [{u}]: {self._source(claim, u)}" for u in claim.source_unit_ids if u in self.unit_text)
                out.append(Entry(ref, header, claim, f"[{claim.claim_id}] ({claim.kind.value}) {claim.text}\n{sources}"))
        return out

    def _source(self, claim: Claim, unit_id: str) -> str:
        """The passage, or only this slot's part of it when the passage was split between slots."""
        spans = [s for s in claim.spans if s.unit_id == unit_id]
        if not spans:
            return self.unit_text[unit_id]
        full = _norm(self.unit_text[unit_id])
        return "(part) " + " ".join(full[s.start:s.end] for s in spans)

    def chunks(self, entries: list[Entry]) -> list[list[Entry]]:
        budget = self.block_tokens * _CHARS_PER_TOKEN
        chunks: list[list[Entry]] = []
        size = 0
        for entry in entries:
            if chunks and size + len(entry.text) <= budget:
                chunks[-1].append(entry)
                size += len(entry.text)
            else:
                chunks.append([entry])
                size = len(entry.text)
        return chunks

    def prompt(self, draft: SectionDraft, chunk: list[Entry]) -> str:
        lines, header = [], None
        for entry in chunk:
            if entry.slot_header != header:
                header = entry.slot_header
                lines.append(header)
            lines.append(entry.text)
        target = self.sections.get(draft.target_section_id)
        name = f"{draft.target_section_id} ({target.heading})" if target else draft.target_section_id
        rules = "\n".join(f"{r.rule_id}: {r.text}" for r in self.rules) or "(none)"
        return USER_PROMPT.format(rules=rules, section=name, claims="\n".join(lines))

    def issues(self, draft: SectionDraft, chunk: list[Entry], output: CriticOutput) -> list[ValidationIssue]:
        shown = {e.claim.claim_id: e for e in chunk}
        out = []
        for finding in output.findings:
            ids = [c for c in dict.fromkeys(finding.claim_ids) if c in shown]
            if not ids:
                continue
            severity = _SEVERITY[finding.severity]
            if finding.problem in _SOFT and severity == Severity.HIGH:
                severity = Severity.MEDIUM
            slots = {s.slot_id for s in draft.slots for c in s.claims if c.claim_id in ids}
            units = list(dict.fromkeys(u for c in ids for u in shown[c].claim.source_unit_ids))
            key = f"critic|{draft.target_section_id}|{','.join(sorted(ids))}|{finding.problem}"
            out.append(ValidationIssue(
                issue_id=f"ISS-{hashlib.sha1(key.encode()).hexdigest()[:10]}", severity=severity,
                category=CATEGORY[finding.problem], source=IssueSource.CRITIC, target_section_id=draft.target_section_id,
                slot_id=slots.pop() if len(slots) == 1 else None, claim_ids=ids, unit_ids=units,
                message=f"[critic:{finding.problem}] {', '.join(ids)}: {re.sub(r'\s+', ' ', finding.explanation).strip()[:400]}",
            ))
        return out


async def _review(critic: Critic, draft: SectionDraft, chunk: list[Entry], chain: Any, rate_limiter: Any,
                  run: CriticRun) -> None:
    prompt = critic.prompt(draft, chunk)
    usage: dict[str, int] = {}
    for attempt in range(2):
        note = "" if attempt == 0 else "\n\nYour previous answer could not be read. Answer with the findings schema only."
        try:
            output = await _call(chain, [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt + note)],
                                 CriticOutput, usage, rate_limiter, "critic")
        except Exception as exc:
            logger.warning(f"Critic call failed for {draft.target_section_id} (attempt {attempt + 1}): {exc}")
            continue
        run.issues += critic.issues(draft, chunk, output)
        break
    else:
        run.failed.append(draft.target_section_id)
        key = f"critic_failed|{draft.target_section_id}|{','.join(e.claim.claim_id for e in chunk)}"
        run.issues.append(ValidationIssue(
            issue_id=f"ISS-{hashlib.sha1(key.encode()).hexdigest()[:10]}", severity=Severity.LOW,
            category=IssueCategory.SEMANTIC, source=IssueSource.CRITIC, target_section_id=draft.target_section_id,
            claim_ids=[e.claim.claim_id for e in chunk],
            message=f"[critic:unavailable] The critic could not review {len(chunk)} reworded claim(s) of "
                    f"{draft.target_section_id}; read them against the source."))
    for k, v in usage.items():
        run.usage[k] = run.usage.get(k, 0) + v


async def critique(critic: Critic, drafts: list[SectionDraft], chain_factory: Any, rate_limiter: Any = None,
                   max_concurrent: int = 3, skip: Optional[dict[str, set[str]]] = None) -> CriticRun:
    """Review the reworded claims of ``drafts``: one call per section (more for a section over the block budget).

    ``skip`` maps a section to claim IDs reviewed in an earlier round (unchanged since): they are not sent again.
    """
    run = CriticRun()
    skip = skip or {}
    work = [(d, chunk) for d in drafts
            for chunk in critic.chunks(critic.entries(d, frozenset(skip.get(d.target_section_id, ()))))]
    if not work or chain_factory is None:
        return run
    chain = chain_factory.create_structured_planner(CriticOutput, include_raw=True)
    gate = asyncio.Semaphore(max(1, max_concurrent))

    async def one(draft: SectionDraft, chunk: list[Entry]) -> None:
        async with gate:
            await _review(critic, draft, chunk, chain, rate_limiter, run)

    await asyncio.gather(*(one(d, c) for d, c in work))
    run.reviewed = list(dict.fromkeys(d.target_section_id for d, _ in work))
    run.issues = sorted({i.issue_id: i for i in run.issues}.values(), key=lambda i: (i.target_section_id or "", i.issue_id))
    return run


def claim_signature(claim: Claim) -> tuple[str, tuple[str, ...]]:
    """What the critic judged: the text and its passages. Survives the renumbering of a rebuilt section."""
    return _norm(claim.text), tuple(claim.source_unit_ids)


__all__ = ["PROMPT_VERSION", "Critic", "claim_signature", "CriticFinding", "CriticOutput", "CriticRun", "critique", "critic_problem",
           "NOT_REPAIRABLE"]

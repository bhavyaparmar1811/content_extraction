"""Level 3 drafter (Phase 9): approved slot plan → ``SectionDraft``s of cited claims.

Per slot, the slot plan's ``migration_action`` decides:

- ``copy_verbatim`` (placement mode, and every table slot): each passage becomes a claim as written.
- ``extract_and_rewrite`` (a GWP is named): text passages are rewritten by the LLM, one call per
  token-bounded block, with the slot's approved STY and PRES rules from the job's rule set and a
  bounded memory (terms, role names, reference targets). Tables, figures, captions and headings are
  never rewritten.

A text passage that feeds several slots of a section (``extraction_scope``, e.g. one APPLICABILITY
sentence naming the roles, the units and the geography) is first split between them: one small LLM
call cuts it into consecutive verbatim pieces and gives each piece to a slot. The split is accepted
only if the pieces, read in order, are the whole passage (nothing dropped, added or repeated; only
spaces and punctuation between pieces may go) and every slot gets a piece. Each slot then drafts its
own part (copied, or rewritten with a GWP), and its claims carry the part as ``spans``. A passage that
cannot be split goes whole into each of its slots, with a note for the reviewer.

Deterministic on every path:
- internal cross-references are ``{{ref:...}}`` tokens before drafting (``refs.py``);
- each source sub-heading inside a template section becomes a heading claim (the template provides
  the section heading itself);
- claims follow the source order inside a slot, except a slot's ``below_unit_ids`` (narrative and
  figures of a table section), which come after its rows (``Drafter.position``);
- callout kinds come from the slot plan's ``callout_assignments``, never from the LLM; promoted
  passages are drafted once, in their content slot;
- a ``source_content_not_found`` slot gets a gap marker; nothing is generated for it.

Rewrites are checked per passage (citations, coverage, reference tokens and the Phase 6 preservation
comparators). Valid claims are kept; only the failed passages get a second, targeted attempt, and a
passage that fails again is copied verbatim and listed in ``unresolved_items``. So a rewritten draft
never loses a passage, a number, a reference or an obligation the checks can see.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.schemas.v2 import (
    CalloutKind,
    Claim,
    ClaimKind,
    DraftOrigin,
    EvidenceSpan,
    FormattingProfile,
    GwpRule,
    GwpRuleSet,
    MappingStatus,
    MigrationAction,
    ProtectedFacts,
    RuleCategory,
    RuleCheck,
    SectionDraft,
    SectionPlan,
    SectionSlotPlan,
    Severity,
    SlotDraft,
    SlotMapping,
    SlotPlan,
    SourceDocument,
    SourceSection,
    SourceUnit,
    TargetSection,
    TargetSlot,
    TemplateModel,
    UnitType,
)
from app.services.llm.prompts.v2.drafter import (
    PROMPT_VERSION,
    REWRITE_SYSTEM_PROMPT,
    REWRITE_USER_PROMPT,
    SPLIT_SYSTEM_PROMPT,
    SPLIT_USER_PROMPT,
)

from ..planning.budget import Block, Line, SectionLines, pack
from ..planning.slot_signals import is_callout_slot, is_table_row, is_table_slot
from ..quality.preservation import check_preservation
from .memory import DEFAULT_MEMORY_TOKENS, build_memory, target_of
from .refs import detokenized, internal_refs, tokenize, tokens_of

RETRY_HEADING = "SECOND ATTEMPT: these passages failed the checks last time; fix exactly this:"
DEFAULT_BLOCK_TOKENS = 1500  # source passage tokens per rewrite call; gpt-4o skipped passages in larger blocks
DEFAULT_MAX_RULES = 40
GAP_TEXT = "Source content not found"
_HINT_CHARS = 150
_WORDISH = re.compile(r"[^\W_]")  # a letter or digit: what a split may never skip
BELOW_OFFSET = 10 ** 9  # sorts the passages shown below a section's tables after every row


def below_units(slot_plan: Optional[SlotPlan]) -> set[tuple[str, str]]:
    """(slot_id, unit_id) of the passages a slot shows after its rows."""
    if slot_plan is None:
        return set()
    return {(m.slot_id, u) for s in slot_plan.sections for m in s.slot_mappings for u in m.below_unit_ids}

_TEXT_TYPES = (UnitType.PARAGRAPH, UnitType.BULLET, UnitType.PROCEDURE_STEP, UnitType.NOTE, UnitType.WARNING,
               UnitType.HEADING_STATEMENT, UnitType.REFERENCE, UnitType.DEFINITION)


def unit_kind(unit: SourceUnit) -> ClaimKind:
    if is_table_row(unit):
        return ClaimKind.TABLE_ROW
    if unit.unit_type == UnitType.FIGURE:
        return ClaimKind.FIGURE
    if unit.unit_type == UnitType.CAPTION:
        return ClaimKind.CAPTION
    if unit.unit_type == UnitType.PROCEDURE_STEP or (unit.unit_type == UnitType.BULLET and unit.list_number):
        return ClaimKind.STEP
    if unit.unit_type == UnitType.BULLET:
        return ClaimKind.BULLET
    return ClaimKind.PARAGRAPH


def rewritable(unit: SourceUnit) -> bool:
    return unit.unit_type in _TEXT_TYPES and not is_table_row(unit)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def split_passage(passage: str, pieces: list[tuple[str, str]], slot_refs: list[str]) -> Optional[dict[str, list[tuple[int, int]]]]:
    """slot ref → (start, end) spans of ``_norm(passage)``, or None when *pieces* are not a clean split.

    Clean: each piece is verbatim and they follow each other in passage order; between pieces, and after the last,
    there is only space and punctuation; every piece names one of *slot_refs* and every slot gets a piece. A comma
    or semicolon at the edge of a piece is left out of it; consecutive pieces of one slot are merged.
    """
    text = _norm(passage)
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    pos, last = 0, None
    for ref, piece in pieces:
        piece = _norm(piece).strip(" ,;")
        if not piece:
            continue
        if ref not in slot_refs:
            return None
        i = text.find(piece, pos)
        if i < 0 or _WORDISH.search(text[pos:i]):
            return None
        if ref == last:
            out[ref][-1] = (out[ref][-1][0], i + len(piece))
        else:
            out[ref].append((i, i + len(piece)))
        pos, last = i + len(piece), ref
    if _WORDISH.search(text[pos:]) or set(out) != set(slot_refs):
        return None
    return dict(out)


# ── LLM output schemas ────────────────────────────────────────────────


class DraftedClaim(BaseModel):
    text: str
    source_unit_ids: list[str] = Field(description="Passage IDs of this slot the claim comes from")
    kind: Literal["paragraph", "bullet", "step"] = "paragraph"
    rule_ids_applied: list[str] = Field(default_factory=list)


class DraftedSlot(BaseModel):
    slot_ref: str = Field(description="The slot ref as given, e.g. 'PURPOSE.what'")
    claims: list[DraftedClaim] = Field(default_factory=list)


class RewriteOutput(BaseModel):
    slots: list[DraftedSlot] = Field(default_factory=list)


class SplitPiece(BaseModel):
    slot_ref: str = Field(description="The slot ref as given, e.g. 'APPLICABILITY.roles'")
    text: str = Field(description="A continuous piece of the passage, word for word")


class Split(BaseModel):
    request_id: str
    pieces: list[SplitPiece] = Field(default_factory=list, description="In passage order; together the whole passage")


class SplitOutput(BaseModel):
    splits: list[Split] = Field(default_factory=list)


# ── Working state ─────────────────────────────────────────────────────


@dataclass
class Piece:
    """One planned passage in one slot."""

    target_section_id: str
    slot_id: str
    unit: SourceUnit
    text: str                     # tokenized; after a split, only this slot's part
    scope: Optional[str] = None   # slot key while the passage feeds several slots unsplit
    mode: str = "copy"            # copy | rewrite | split (shared, waiting to be split; then copy or rewrite)
    rewrite: bool = False         # the slot plan rewrites this passage (GWP)
    spans: list[EvidenceSpan] = field(default_factory=list)  # this slot's part of the passage, once split


@dataclass
class Drafted:
    """A claim before its ID: sorted by position (first cited unit's reading order, then answer order)."""

    position: tuple[int, int]
    text: str
    kind: ClaimKind
    unit_ids: list[str]
    rule_ids: list[str] = field(default_factory=list)
    spans: list[EvidenceSpan] = field(default_factory=list)


@dataclass
class SlotJob:
    section: TargetSection
    slot: TargetSlot
    mapping: SlotMapping
    pieces: list[Piece]
    gap: bool = False

    @property
    def ref(self) -> str:
        return f"{self.section.key or self.section.section_id}.{self.slot.key or self.slot.slot_id}"


@dataclass
class SectionJob:
    section: TargetSection
    plan: Optional[SectionSlotPlan]
    slots: list[SlotJob] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


@dataclass
class DraftRun:
    sections: list[SectionJob]
    drafted: dict[str, list[Drafted]] = field(default_factory=lambda: defaultdict(list))  # slot_id → claims
    usage: dict[str, int] = field(default_factory=lambda: {"calls": 0})
    llm_used: bool = False

    def job(self, slot_id: str) -> Optional[SlotJob]:
        return next((j for s in self.sections for j in s.slots if j.slot.slot_id == slot_id), None)

    def section_of(self, slot_id: str) -> Optional[SectionJob]:
        return next((s for s in self.sections if any(j.slot.slot_id == slot_id for j in s.slots)), None)

    def add_usage(self, usage: dict[str, int]) -> None:
        for k, v in usage.items():
            self.usage[k] = self.usage.get(k, 0) + v


class Drafter:
    def __init__(self, source: SourceDocument, template: TemplateModel, section_plan: SectionPlan, slot_plan: SlotPlan,
                 facts: ProtectedFacts, rules: Optional[GwpRuleSet] = None, max_rules: int = DEFAULT_MAX_RULES,
                 memory_tokens: int = DEFAULT_MEMORY_TOKENS):
        self.source = source
        self.template = template
        self.section_plan = section_plan
        self.slot_plan = slot_plan
        self.facts = facts
        self.rules = rules
        self.max_rules = max_rules
        self.memory_tokens = memory_tokens
        self.unit_by_id = {u.unit_id: u for u in source.iter_units()}
        self.section_by_id = {s.section_id: s for s in source.sections}
        self.refs = internal_refs(source)
        self.rule_by_id = {r.rule_id: r for r in rules.rules} if rules else {}
        self.section_map = target_of(section_plan, source, template)
        self.below = below_units(slot_plan)

    def position(self, slot_id: str, unit_id: str) -> int:
        """A passage's place in its slot: its reading order; passages shown below the tables come after the rows."""
        return self.unit_by_id[unit_id].seq + (BELOW_OFFSET if (slot_id, unit_id) in self.below else 0)

    # ── 1. What to draft ──────────────────────────────────────────────

    def prepare(self) -> DraftRun:
        sections = []
        for target in self.template.sections:
            plan = self.slot_plan.section(target.section_id)
            job = SectionJob(target, plan)
            if plan is None:
                sections.append(job)
                continue
            mappings = {m.slot_id: m for m in plan.slot_mappings}
            shared = self._shared_units(plan, target)
            in_content = {u for m in plan.slot_mappings for u in m.source_unit_ids
                          if (s := self._slot(target, m.slot_id)) is not None and not is_callout_slot(s)}
            for slot in sorted(target.slots, key=lambda s: s.display_order):
                m = mappings.get(slot.slot_id)
                if m is None:
                    continue
                if m.status == MappingStatus.SOURCE_CONTENT_NOT_FOUND:
                    job.slots.append(SlotJob(target, slot, m, [], gap=True))
                    continue
                units = [u for u in m.source_unit_ids if u in self.unit_by_id]
                if is_callout_slot(slot):
                    units = [u for u in units if u not in in_content]  # promoted passages are drafted in place
                if not units:
                    continue
                pieces = [self._piece(target, slot, m, self.unit_by_id[u], u in shared) for u in units]
                job.slots.append(SlotJob(target, slot, m, pieces))
            # A passage shared with a table or callout slot only has a single text slot: nothing to split.
            waiting = defaultdict(list)
            for j in job.slots:
                for p in j.pieces:
                    if p.mode == "split":
                        waiting[p.unit.unit_id].append(p)
            for group in waiting.values():
                if len(group) == 1:
                    group[0].mode, group[0].scope = ("rewrite" if group[0].rewrite else "copy"), None
            job.unresolved += [f"{u}: not placed in any slot (slot plan); not drafted" for u in plan.unplaced_unit_ids]
            sections.append(job)
        return DraftRun(sections)

    @staticmethod
    def _slot(target: TargetSection, slot_id: str) -> Optional[TargetSlot]:
        return next((s for s in target.slots if s.slot_id == slot_id), None)

    def _shared_units(self, plan: SectionSlotPlan, target: TargetSection) -> set[str]:
        """Passages in more than one content slot (a fixed callout box repeating a passage does not count)."""
        count: dict[str, int] = defaultdict(int)
        for m in plan.slot_mappings:
            slot = self._slot(target, m.slot_id)
            if slot is None or is_callout_slot(slot):
                continue
            for u in m.source_unit_ids:
                count[u] += 1
        return {u for u, n in count.items() if n > 1}

    def _piece(self, target: TargetSection, slot: TargetSlot, m: SlotMapping, unit: SourceUnit, shared: bool) -> Piece:
        scope = (m.extraction_scope[0] if m.extraction_scope else slot.key) if shared else None
        text_slot = rewritable(unit) and not is_table_slot(slot)
        rewrite = text_slot and m.migration_action in (MigrationAction.EXTRACT_AND_REWRITE, MigrationAction.EXTRACT_AND_CONSOLIDATE,
                                                       MigrationAction.REWRITE_AS_ORDERED_PROCEDURE)
        if not text_slot:
            mode, scope = "copy", None
        else:
            mode = "split" if scope else "rewrite" if rewrite else "copy"
        return Piece(target.section_id, slot.slot_id, unit, tokenize(unit, self.refs.get(unit.unit_id, [])), scope, mode,
                     rewrite)

    # ── 2. Deterministic claims ───────────────────────────────────────

    def copy(self, run: DraftRun, pieces: list[Piece], note: Optional[str] = None) -> None:
        for piece in pieces:
            run.drafted[piece.slot_id].append(Drafted((self.position(piece.slot_id, piece.unit.unit_id), 0), piece.text,
                                                      unit_kind(piece.unit), [piece.unit.unit_id],
                                                      spans=list(piece.spans)))
            if note:
                run.section_of(piece.slot_id).unresolved.append(f"{piece.unit.unit_id}: {note}")

    def draft_copies(self, run: DraftRun, llm_available: bool) -> None:
        """Every piece that needs no LLM, and the LLM-bound ones when no LLM is configured."""
        for section in run.sections:
            for job in section.slots:
                copies = [p for p in job.pieces if p.mode == "copy"]
                self.copy(run, copies)
                if not llm_available:
                    self.copy(run, [p for p in job.pieces if p.mode == "rewrite"], "no LLM configured: copied verbatim, not rewritten")
                    self.copy(run, [p for p in job.pieces if p.mode == "split"],
                              f"no LLM configured: the whole passage is copied into {job.ref}, not only its part")

    # ── 3. Rewrite (GWP) ──────────────────────────────────────────────

    def drafter_rules(self, rule_ids: list[str]) -> list[GwpRule]:
        """The approved STY and PRES rules among the slots' rule IDs: checkable, duplicates dropped, capped."""
        picked: list[GwpRule] = []
        seen: list[set[str]] = []
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}
        candidates = [self.rule_by_id[r] for r in dict.fromkeys(rule_ids) if r in self.rule_by_id]
        candidates = [r for r in candidates if r.category in (RuleCategory.STYLE, RuleCategory.PRESERVATION)
                      and r.check != RuleCheck.NONE]
        for rule in sorted(candidates, key=lambda r: (r.category != RuleCategory.PRESERVATION, order[r.severity])):
            words = set(re.findall(r"[a-z]{3,}", rule.text.lower()))
            if any(len(words & w) / max(1, len(words | w)) >= 0.6 for w in seen):
                continue
            seen.append(words)
            picked.append(rule)
        return sorted(picked[: self.max_rules], key=lambda r: r.rule_id)

    def rewrite_blocks(self, run: DraftRun, budget: int = DEFAULT_BLOCK_TOKENS) -> list[Block]:
        parts = []
        for section in run.sections:
            for job in section.slots:
                pieces = [p for p in job.pieces if p.mode == "rewrite"]
                if not pieces:
                    continue
                parts.append(self._slot_lines(job, pieces))
        return pack(parts, budget)

    def _slot_lines(self, job: SlotJob, pieces: list[Piece]) -> SectionLines:
        header = f"### {job.ref} | {job.slot.content_type.value} | {_clip(job.slot.instruction, _HINT_CHARS)}"
        lines = []
        for p in pieces:
            part = f" (part: {p.scope})" if p.scope else ""
            lines.append(Line(f"[{p.unit.unit_id}] ({unit_kind(p.unit).value}){part} {p.text}", [p.unit.unit_id],
                              break_ok=p.unit.unit_type not in (UnitType.BULLET, UnitType.PROCEDURE_STEP)))
        return SectionLines(job.slot.slot_id, header, lines)

    def block_rules(self, block: Block, run: DraftRun) -> list[GwpRule]:
        ids = [r for slot_id in block.target_section_ids for r in run.job(slot_id).mapping.rule_ids]
        return self.drafter_rules(ids)

    def rewrite_prompt(self, block: Block, run: DraftRun, previous: Optional[dict[tuple[str, str], str]] = None,
                       heading: str = RETRY_HEADING) -> str:
        rules = self.block_rules(block, run)
        texts = [line.text for part in block.parts for line in part.lines]
        refs = [t for text in texts for t in tokens_of(text)]
        memory = build_memory(texts, self.facts, self.section_map, refs).render(self.memory_tokens)
        prompt = REWRITE_USER_PROMPT.format(
            rules="\n".join(f"{r.rule_id}: {r.text}" for r in rules) or "(none)",
            memory=memory, slots=block.render(),
        )
        if previous:
            order = sorted(previous.items(), key=lambda kv: self.unit_by_id[kv[0][1]].seq)
            prompt += (f"\n\n{heading}\n"
                       + "\n".join(f"- {u}: {reason}" for (_, u), reason in order))
        return prompt

    def _block_pieces(self, block: Block, run: DraftRun) -> dict[str, list[Piece]]:
        out: dict[str, list[Piece]] = {}
        for part in block.parts:
            ids = {u for line in part.lines for u in line.unit_ids}
            out[part.target_section_id] = [p for p in run.job(part.target_section_id).pieces if p.unit.unit_id in ids]
        return out

    def evaluate(self, block: Block, run: DraftRun, output: RewriteOutput) -> tuple[dict[str, list[DraftedClaim]], dict[tuple[str, str], str]]:
        """Split an answer into the claims to keep and the (slot, passage) pairs that failed, with the reason.

        A claim is dropped when it cites nothing, cites a passage outside its slot, or carries a reference token its
        passages do not have. A passage fails when no kept claim cites it, when its claims lost one of its reference
        tokens, or when the Phase 6 comparators find a high or critical change; every claim citing a failed passage
        is dropped, and the passages those claims cite fail with it.
        """
        pieces = self._block_pieces(block, run)
        by_ref = {run.job(sid).ref: sid for sid in pieces}
        kept: dict[str, list[DraftedClaim]] = {sid: [] for sid in pieces}
        failed: dict[tuple[str, str], str] = {}
        answered: set[str] = set()
        for drafted in output.slots:
            slot_id = by_ref.get(drafted.slot_ref.strip())
            if slot_id is None or slot_id in answered:
                continue
            answered.add(slot_id)
            expected = {p.unit.unit_id: p for p in pieces[slot_id]}
            for claim in drafted.claims:
                units = list(dict.fromkeys(claim.source_unit_ids))
                if not claim.text.strip() or not units:
                    continue
                stray = [u for u in units if u not in expected]
                own = {t for u in units if u in expected for t in tokens_of(expected[u].text)}
                foreign = [t for t in tokens_of(claim.text) if t not in own]
                if stray or foreign:
                    for u in units:
                        if u in expected:
                            failed.setdefault((slot_id, u), f"a claim cited {stray} outside the slot" if stray
                                              else f"a claim carried foreign reference tokens {foreign}")
                    continue
                kept[slot_id].append(claim.model_copy(update={"source_unit_ids": units}))
        for slot_id, slot_pieces in pieces.items():
            claims = kept[slot_id]
            for p in slot_pieces:
                u = p.unit.unit_id
                mine = [c for c in claims if u in c.source_unit_ids]
                if not mine:
                    failed.setdefault((slot_id, u), "not cited by any claim")
                    continue
                lost = [t for t in tokens_of(p.text) if t not in {x for c in mine for x in tokens_of(c.text)}]
                if lost:
                    failed.setdefault((slot_id, u), f"lost the reference token(s) {lost}")
        for (slot_id, u), reason in self._preservation_failures(block, run, kept, pieces).items():
            failed.setdefault((slot_id, u), reason)
        # Drop every claim that cites a failed passage; its other passages fail with it.
        changed = True
        while changed:
            changed = False
            for slot_id in kept:
                bad = {u for (s, u) in failed if s == slot_id}
                for claim in [c for c in kept[slot_id] if bad & set(c.source_unit_ids)]:
                    kept[slot_id].remove(claim)
                    for u in claim.source_unit_ids:
                        if (slot_id, u) not in failed:
                            failed[(slot_id, u)] = "merged into a claim with a failed passage"
                            changed = True
        return kept, failed

    def _preservation_failures(self, block: Block, run: DraftRun, kept: dict[str, list[DraftedClaim]],
                               pieces: dict[str, list[Piece]]) -> dict[tuple[str, str], str]:
        """Phase 6 comparators on the kept claims, for passages drafted entirely in this block."""
        slots = [SlotDraft(slot_id=sid, claims=[Claim(claim_id=f"B-{sid}-{i}", text=c.text, source_unit_ids=c.source_unit_ids)
                                                for i, c in enumerate(claims)]) for sid, claims in kept.items()]
        draft = SectionDraft(target_section_id="BLOCK", version=1, slots=slots)
        here = {(sid, p.unit.unit_id) for sid, ps in pieces.items() for p in ps}
        complete = {u for (_, u) in here
                    if all((j.slot.slot_id, p.unit.unit_id) in here
                           for s in run.sections for j in s.slots for p in j.pieces if p.unit.unit_id == u)}
        slot_of = defaultdict(list)
        for sid, u in here:
            slot_of[u].append(sid)
        out: dict[tuple[str, str], str] = {}
        for issue in check_preservation(self.facts, self.source, detokenized([draft], self.source)):
            if issue.severity not in (Severity.CRITICAL, Severity.HIGH):
                continue
            for u in issue.unit_ids:
                if u in complete:
                    for sid in slot_of[u]:
                        out.setdefault((sid, u), issue.message)
        return out

    def keep(self, block: Block, run: DraftRun, kept: dict[str, list[DraftedClaim]]) -> None:
        allowed = {r.rule_id for r in self.block_rules(block, run)}
        pieces = self._block_pieces(block, run)
        for slot_id, claims in kept.items():
            seq = {p.unit.unit_id: self.position(slot_id, p.unit.unit_id) for p in pieces[slot_id]}
            spans = {p.unit.unit_id: p.spans for p in pieces[slot_id]}
            for i, claim in enumerate(claims):
                run.drafted[slot_id].append(Drafted(
                    (min(seq[u] for u in claim.source_unit_ids), i), _norm(claim.text), ClaimKind(claim.kind),
                    list(claim.source_unit_ids), [r for r in claim.rule_ids_applied if r in allowed],
                    [s for u in claim.source_unit_ids for s in spans[u]],
                ))

    def sub_block(self, run: DraftRun, failed: dict[tuple[str, str], str]) -> Block:
        """A block of only the failed passages, for the second attempt."""
        parts = []
        for section in run.sections:
            for job in section.slots:
                pieces = [p for p in job.pieces if (job.slot.slot_id, p.unit.unit_id) in failed]
                if pieces:
                    parts.append(self._slot_lines(job, pieces))
        return pack(parts, 10 ** 9)[0]

    def copy_failed(self, run: DraftRun, failed: dict[tuple[str, str], str],
                    why: str = "the rewrite failed its checks twice") -> None:
        """Passages that failed both attempts are copied verbatim: one note per slot."""
        by_slot: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for (slot_id, u), reason in failed.items():
            by_slot[slot_id].append((u, reason))
        for slot_id, items in by_slot.items():
            job = run.job(slot_id)
            ids = {u for u, _ in items}
            self.copy(run, [p for p in job.pieces if p.unit.unit_id in ids])
            reasons = "; ".join(dict.fromkeys(r for _, r in items))
            run.section_of(slot_id).unresolved.append(
                f"{job.ref}: {len(items)} passage(s) copied verbatim, {why} "
                f"({_clip(reasons, 300)}): {', '.join(sorted(ids, key=lambda u: self.unit_by_id[u].seq))}")

    def restore(self, run: DraftRun, drafts: list[SectionDraft]) -> None:
        """Load saved drafts back into a prepared run, so a repair can rebuild a section around the passages it re-drafts.

        Headings, gap markers and callout kinds are left out: ``build_drafts`` derives them again. Each claim keeps
        its text, kind, rules and spans; its position is its first passage's reading order, so a rebuilt slot is back
        in source order. A split passage's piece becomes the slot's part again (from the claims' spans), so a repair
        drafts that part, not the whole passage.
        """
        by_section = {d.target_section_id: d for d in drafts}
        for section in run.sections:
            draft = by_section.get(section.section.section_id)
            if draft is None:
                continue
            section.unresolved = list(draft.unresolved_items)
            planned = {j.slot.slot_id: {p.unit.unit_id for p in j.pieces} for j in section.slots}
            for slot in draft.slots:
                for i, claim in enumerate(slot.claims):
                    # Only citations of the slot's planned passages survive; a claim left with none is dropped.
                    units = [u for u in claim.source_unit_ids if u in planned.get(slot.slot_id, ())]
                    if claim.is_gap_marker or claim.kind == ClaimKind.HEADING or not units:
                        continue
                    run.drafted[slot.slot_id].append(Drafted((min(self.position(slot.slot_id, u) for u in units), i), claim.text,
                                                             claim.kind, units, list(claim.rule_ids_applied), list(claim.spans)))
                    for piece in (p for p in run.job(slot.slot_id).pieces if p.mode == "split" and p.unit.unit_id in units):
                        part = [s for s in claim.spans if s.unit_id == piece.unit.unit_id]
                        if part:
                            self._take_part(piece, part)

    # ── 4. Splits (a passage shared by several slots) ─────────────────

    @staticmethod
    def _take_part(piece: Piece, spans: list[EvidenceSpan]) -> None:
        """The piece becomes its slot's part of the passage, drafted like any other piece from now on."""
        full = _norm(piece.text)
        piece.text = " ".join(full[s.start:s.end] for s in spans)
        piece.spans, piece.scope = list(spans), None
        piece.mode = "rewrite" if piece.rewrite else "copy"

    def split_requests(self, run: DraftRun) -> list[tuple[str, list[tuple[Piece, SlotJob]]]]:
        """One request per shared passage: its pieces with their slots, in the template's slot order."""
        groups: dict[tuple[str, str], list[tuple[Piece, SlotJob]]] = {}
        for section in run.sections:
            for job in section.slots:
                for piece in job.pieces:
                    if piece.mode == "split":
                        groups.setdefault((section.section.section_id, piece.unit.unit_id), []).append((piece, job))
        return [(f"R{i + 1}", members) for i, members in enumerate(groups.values())]

    def split_prompt(self, requests: list[tuple[str, list[tuple[Piece, SlotJob]]]]) -> str:
        blocks = []
        for rid, members in requests:
            piece = members[0][0]
            lines = [f"{rid}: passage [{piece.unit.unit_id}]: {_norm(piece.text)}", "    slots:"]
            lines += [f"    - {job.ref}: {_clip(job.slot.instruction, _HINT_CHARS)}" for _, job in members]
            blocks.append("\n".join(lines))
        return SPLIT_USER_PROMPT.format(requests="\n\n".join(blocks))

    def accept_splits(self, run: DraftRun, requests: list[tuple[str, list[tuple[Piece, SlotJob]]]],
                      output: Optional[SplitOutput]) -> list[str]:
        """Apply the clean splits; a passage without one goes whole into each of its slots. Returns the rejected IDs."""
        answers = {s.request_id: s for s in (output.splits if output else [])}
        rejected = []
        for rid, members in requests:
            unit = members[0][0].unit
            refs = [job.ref for _, job in members]
            answer = answers.get(rid)
            cut = split_passage(members[0][0].text, [(p.slot_ref.strip(), p.text) for p in answer.pieces], refs) if answer else None
            if cut is None:
                rejected.append(rid)
                run.section_of(members[0][0].slot_id).unresolved.append(
                    f"{unit.unit_id}: the passage could not be split between {', '.join(refs)}; each slot gets it whole")
            for piece, job in members:
                if cut is not None:
                    self._take_part(piece, [EvidenceSpan(unit_id=unit.unit_id, start=a, end=b) for a, b in cut[job.ref]])
                else:
                    piece.mode = "rewrite" if piece.rewrite else "copy"  # whole; a rewrite still sees "(part: X)"
                if piece.mode == "copy":
                    self.copy(run, [piece])
        return rejected

    # ── 5. Assemble the drafts ────────────────────────────────────────

    def _heading_slots(self, section: TargetSection) -> set[str]:
        """Source sub-headings are kept only in a section's single free-text slot."""
        content = [s for s in section.slots if not is_callout_slot(s)]
        return {content[0].slot_id} if len(content) == 1 and not is_table_slot(content[0]) else set()

    def _sub_sections(self, unit: SourceUnit) -> list[SourceSection]:
        """The unit's source section and its ancestors below the chapter (outermost first)."""
        chain = []
        node = self.section_by_id.get(unit.section_id)
        while node is not None and node.parent_id and node.parent_id in self.section_by_id:
            chain.append(node)
            node = self.section_by_id[node.parent_id]
        return list(reversed(chain))

    def build_drafts(self, run: DraftRun, versions: dict[str, int], origin: DraftOrigin = DraftOrigin.LLM,
                     model: Optional[str] = None) -> list[SectionDraft]:
        drafts = []
        for section in run.sections:
            if section.plan is None:
                continue
            kinds = {u: a.kind for a in section.plan.callout_assignments for u in a.unit_ids}
            heading_slots = self._heading_slots(section.section)
            counter = 0
            slots = []
            for job in section.slots:
                rendering = (FormattingProfile.TABLE if is_table_slot(job.slot) else
                             job.slot.formatting_profile if job.slot.formatting_profile not in (None, FormattingProfile.CALLOUT)
                             else FormattingProfile.PARAGRAPHS)
                claims: list[Claim] = []
                if job.gap:
                    counter += 1
                    claims.append(Claim(claim_id=f"C-{section.section.section_id}-{counter:03d}", text=GAP_TEXT, is_gap_marker=True))
                emitted: set[str] = set()
                for d in sorted(run.drafted.get(job.slot.slot_id, []), key=lambda d: d.position):
                    if job.slot.slot_id in heading_slots:
                        for sub in self._sub_sections(self.unit_by_id[d.unit_ids[0]]):
                            if sub.section_id in emitted:
                                continue
                            emitted.add(sub.section_id)
                            counter += 1
                            claims.append(Claim(
                                claim_id=f"C-{section.section.section_id}-{counter:03d}", kind=ClaimKind.HEADING,
                                text=_norm(sub.heading), source_section_id=sub.section_id,
                                list_level=max(0, sub.level - 2),
                            ))
                    counter += 1
                    unit_kinds = {kinds.get(u) for u in d.unit_ids}
                    callout = next(iter(unit_kinds)) if len(unit_kinds) == 1 else None
                    if callout is None and job.slot.callout_kind is not None:
                        callout = job.slot.callout_kind
                    unit = self.unit_by_id[d.unit_ids[0]]
                    claims.append(Claim(
                        claim_id=f"C-{section.section.section_id}-{counter:03d}", text=d.text, kind=d.kind,
                        source_unit_ids=d.unit_ids, spans=d.spans, rule_ids_applied=d.rule_ids, callout_kind=callout,
                        list_level=(unit.list_level or 0) if d.kind in (ClaimKind.BULLET, ClaimKind.STEP) else 0,
                    ))
                slots.append(SlotDraft(slot_id=job.slot.slot_id, rendering=rendering, claims=claims))
            drafts.append(SectionDraft(
                target_section_id=section.section.section_id, version=versions.get(section.section.section_id, 1),
                origin=origin, slots=slots, unresolved_items=list(dict.fromkeys(section.unresolved)),
                prompt_version=PROMPT_VERSION if run.llm_used else None, model=model if run.llm_used else None,
            ))
        return drafts


# ── LLM calls ─────────────────────────────────────────────────────────


def _usage(raw: Any) -> dict[str, int]:
    meta = getattr(raw, "usage_metadata", None) or {}
    return {k: int(meta.get(k, 0)) for k in ("input_tokens", "output_tokens", "total_tokens") if k in meta}


async def _call(chain: Any, messages: list, schema: type, usage: dict[str, int], rate_limiter: Any, task: str):
    coro = lambda: chain.ainvoke(messages)  # noqa: E731
    result = await (rate_limiter.execute(coro, task_name=task) if rate_limiter else coro())
    usage["calls"] = usage.get("calls", 0) + 1
    if isinstance(result, dict) and "parsed" in result:
        for k, v in _usage(result.get("raw")).items():
            usage[k] = usage.get(k, 0) + v
        if result.get("parsed") is None:
            raise ValueError(f"unparseable {task} output: {result.get('parsing_error')}")
        result = result["parsed"]
    return result if isinstance(result, schema) else schema.model_validate(result)


async def rewrite_block(drafter: Drafter, run: DraftRun, block: Block, chain: Any, rate_limiter: Any = None) -> int:
    """Rewrite one block: keep the valid claims, retry only the failed passages once, copy what still fails.

    Returns the number of passages copied verbatim.
    """
    usage: dict[str, int] = {}
    pending, previous = block, None
    failed: dict[tuple[str, str], str] = {}
    for attempt in range(2):
        messages = [SystemMessage(content=REWRITE_SYSTEM_PROMPT),
                    HumanMessage(content=drafter.rewrite_prompt(pending, run, previous))]
        try:
            output = await _call(chain, messages, RewriteOutput, usage, rate_limiter, "drafter")
        except Exception as exc:  # this block is retried, then copied; the other blocks go on
            logger.warning(f"Drafter call failed for {pending.target_section_ids}: {exc}")
            output = RewriteOutput()
        kept, failed = drafter.evaluate(pending, run, output)
        drafter.keep(pending, run, kept)
        if not failed:
            break
        if attempt == 0:
            logger.info(f"Drafter: {len(failed)} passage(s) failed in {pending.target_section_ids}; one more attempt for them")
            pending, previous = drafter.sub_block(run, failed), failed
    if failed:
        drafter.copy_failed(run, failed)
    run.add_usage(usage)
    return len(failed)


async def draft_all(drafter: Drafter, chain_factory: Any = None, rate_limiter: Any = None,
                    block_tokens: int = DEFAULT_BLOCK_TOKENS, max_concurrent: int = 3) -> DraftRun:
    """Prepare, copy, split shared passages and rewrite: the whole Level 3 run for one job."""
    run = drafter.prepare()
    llm = chain_factory is not None
    drafter.draft_copies(run, llm)
    if not llm:
        return run
    requests = drafter.split_requests(run)
    if requests:
        usage: dict[str, int] = {}
        chain = chain_factory.create_structured_planner(SplitOutput, include_raw=True)
        try:
            output = await _call(chain, [SystemMessage(content=SPLIT_SYSTEM_PROMPT),
                                         HumanMessage(content=drafter.split_prompt(requests))],
                                 SplitOutput, usage, rate_limiter, "drafter_split")
        except Exception as exc:
            logger.warning(f"Drafter split call failed: {exc}")
            output = None
        rejected = drafter.accept_splits(run, requests, output)
        if rejected:
            logger.info(f"Drafter: {len(rejected)} shared passage(s) could not be split; each slot gets them whole")
        run.add_usage(usage)
        run.llm_used = True
    blocks = drafter.rewrite_blocks(run, block_tokens)
    if blocks:
        chain = chain_factory.create_structured_planner(RewriteOutput, include_raw=True)
        gate = asyncio.Semaphore(max(1, max_concurrent))

        async def one(block: Block):
            async with gate:
                return await rewrite_block(drafter, run, block, chain, rate_limiter)

        await asyncio.gather(*(one(b) for b in blocks))
        run.llm_used = True
    return run

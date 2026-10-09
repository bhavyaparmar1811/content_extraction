"""Targeted repair (Phase 10, migration_plan.md §18): re-draft only the passages an issue points at.

Which issues are repaired: open ones of high or critical severity, found by the checks or the critic, except
- a required slot with no source content (``missing_slot``) or a missing section: nothing is generated for a gap;
- the critic's ``source_conflict`` and ``wrong_slot``, and a critic call that failed: a reviewer decides;
- anything in a section a reviewer edited (``origin: human``): their text is never overwritten.

How: the section's saved draft is loaded back into a drafting run (``Drafter.restore``); the claims that cite
an affected passage are dropped (with every passage they also cite), and those passages are drafted again:
- a passage the slot plan copies is copied from the source again (a split passage: the slot's part, from the
  claims' spans; whole, with a note, when the slot has no claim left to take the part from);
- a passage the slot plan rewrites goes to the LLM once, with the issues as the reasons to fix. The drafter's
  per-passage checks run on the answer; what fails them is copied verbatim, so a repair never makes the
  deterministic checks worse;
- on a section's last allowed attempt (``verbatim_sections``) a rewritten passage is copied from the source
  instead: a fresh rewording would only give the critic new wording to flag. Meaning wins over house style
  (migration_plan.md §2.1); a note lists the passages for the reviewer.
The section is then rebuilt: claims back in source order, headings, gap markers and callout kinds derived
again from the slot plan, so an order or callout issue is fixed by the rebuild alone. The result is a new
``SectionDraft`` version with ``origin: repair``.

The stage allows ``repair_max_attempts`` rounds per section (2 by default); then the section needs review.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from app.schemas.v2 import DraftOrigin, Gate, IssueCategory, IssueSource, SectionDraft, Severity, ValidationIssue
from app.services.llm.prompts.v2.drafter import REWRITE_SYSTEM_PROMPT

from ..drafting.drafter import DraftRun, Drafter, RewriteOutput, _call
from .critic import NOT_REPAIRABLE, critic_problem
from .validator import CALLOUT_TAG

REPAIR_HEADING = ("REPAIR: checks of the previous draft found these problems in the passages below. Fix exactly "
                  "these problems and keep everything else of the passage:")
_NOT_REPAIRED_GATES = frozenset({Gate.MISSING_SLOT, Gate.MISSING_SECTION})


def repairable(issue: ValidationIssue) -> bool:
    if issue.resolved or issue.source == IssueSource.HUMAN or issue.gate in _NOT_REPAIRED_GATES:
        return False
    if issue.severity not in (Severity.CRITICAL, Severity.HIGH):
        return False
    problem = critic_problem(issue)
    return problem not in NOT_REPAIRABLE and problem != "unavailable"


def rebuild_fixes(issue: ValidationIssue) -> bool:
    """Order and callout issues need no new text: rebuilding the section sorts the claims and derives the callouts."""
    return issue.category == IssueCategory.ORDER or issue.message.startswith(CALLOUT_TAG)


def claim_locations(drafts: Iterable[SectionDraft]) -> dict[str, tuple[str, str]]:
    """claim_id → (target section, slot)."""
    return {c.claim_id: (d.target_section_id, s.slot_id) for d in drafts for s in d.slots for c in s.claims}


def issue_sections(issue: ValidationIssue, located: dict[str, tuple[str, str]],
                   unit_sections: Optional[dict[str, set[str]]] = None) -> set[str]:
    """The template sections an issue is about: its own, else its claims', else where its passages were drafted."""
    if issue.target_section_id:
        return {issue.target_section_id}
    sections = {located[c][0] for c in issue.claim_ids if c in located}
    if not sections and unit_sections:
        sections = {s for u in issue.unit_ids for s in unit_sections.get(u, set())}
    return sections


@dataclass
class RepairResult:
    drafts: list[SectionDraft] = field(default_factory=list)
    redrafted: dict[str, list[str]] = field(default_factory=dict)  # slot_id → passages drafted again
    usage: dict[str, int] = field(default_factory=lambda: {"calls": 0})
    llm_used: bool = False


def affected_passages(run: DraftRun, issues: list[ValidationIssue], sections: set[str],
                      drafts: list[SectionDraft]) -> dict[tuple[str, str], str]:
    """(slot_id, unit_id) → the reasons to fix, for the planned passages the issues point at in ``sections``.

    An issue points at its passages and at every passage its claims cite, in the slots of those claims (or its
    own slot). A slot-level issue with neither (a planned slot with no draft) points at the whole slot.
    """
    located = claim_locations(drafts)
    claim_units = {c.claim_id: list(c.source_unit_ids) for d in drafts for c in d.iter_claims()}
    pieces: dict[str, set[str]] = {j.slot.slot_id: {p.unit.unit_id for p in j.pieces}
                                   for s in run.sections if s.section.section_id in sections for j in s.slots}
    reasons: dict[tuple[str, str], list[str]] = defaultdict(list)
    for issue in issues:
        if rebuild_fixes(issue):
            continue
        slots = {located[c][1] for c in issue.claim_ids if c in located}
        if issue.slot_id:
            slots.add(issue.slot_id)
        if not slots:
            slots = {sid for sid, units in pieces.items() if units & set(issue.unit_ids)}
        for slot_id in slots & pieces.keys():
            units = set(issue.unit_ids) | {u for c in issue.claim_ids if located.get(c, ("", ""))[1] == slot_id
                                           for u in claim_units[c]}
            units &= pieces[slot_id]
            if not units and not issue.unit_ids and not issue.claim_ids:
                units = set(pieces[slot_id])
            for unit_id in units:
                reasons[(slot_id, unit_id)].append(issue.message)
    return {k: " | ".join(dict.fromkeys(v))[:600] for k, v in reasons.items()}


def _drop(run: DraftRun, affected: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    """Drop the restored claims citing an affected passage; the other passages they cite are drafted again too."""
    affected = dict(affected)
    changed = True
    while changed:
        changed = False
        for slot_id in {s for s, _ in affected}:
            units = {u for s, u in affected if s == slot_id}
            keep = []
            for d in run.drafted.get(slot_id, []):
                if units & set(d.unit_ids):
                    for u in d.unit_ids:
                        if (slot_id, u) not in affected:
                            affected[(slot_id, u)] = "drafted together with a passage being repaired"
                            changed = True
                else:
                    keep.append(d)
            run.drafted[slot_id] = keep
    return affected


async def repair_sections(drafter: Drafter, drafts: list[SectionDraft], issues: list[ValidationIssue], sections: set[str],
                          versions: dict[str, int], chain_factory: Any = None, rate_limiter: Any = None,
                          model: Optional[str] = None, verbatim_sections: Iterable[str] = ()) -> RepairResult:
    """Repair ``sections`` of ``drafts`` against ``issues`` (already filtered to repairable ones).

    In ``verbatim_sections`` (their last attempt) the affected passages are copied from the source, never re-worded.
    """
    verbatim = set(verbatim_sections)
    result = RepairResult()
    run = drafter.prepare()
    drafter.restore(run, drafts)
    affected = _drop(run, affected_passages(run, issues, sections, drafts))

    copies, rewrites = {}, {}
    for (slot_id, unit_id), reason in affected.items():
        job = run.job(slot_id)
        piece = next((p for p in job.pieces if p.unit.unit_id == unit_id), None) if job else None
        if piece is None:
            continue
        last = piece.target_section_id in verbatim
        (rewrites if piece.mode == "rewrite" and chain_factory is not None and not last else copies)[(slot_id, unit_id)] = (piece, reason)
    for (slot_id, _), (piece, reason) in sorted(copies.items(), key=lambda kv: kv[1][0].unit.seq):
        note = None
        if piece.mode == "split":
            note = f"repair: the whole passage is copied into {run.job(slot_id).ref}, not only its part"
        elif piece.mode == "rewrite" and piece.target_section_id in verbatim:
            note = f"copied verbatim on the last repair attempt, the rewrite still had problems ({reason[:200]})"
        elif piece.mode == "rewrite":
            note = "repair without an LLM: copied verbatim, not rewritten"
        drafter.copy(run, [piece], note)

    if rewrites:
        failed = {k: reason for k, (_, reason) in rewrites.items()}
        block = drafter.sub_block(run, failed)
        messages = [SystemMessage(content=REWRITE_SYSTEM_PROMPT),
                    HumanMessage(content=drafter.rewrite_prompt(block, run, failed, heading=REPAIR_HEADING))]
        chain = chain_factory.create_structured_planner(RewriteOutput, include_raw=True)
        try:
            output = await _call(chain, messages, RewriteOutput, result.usage, rate_limiter, "repair")
        except Exception as exc:  # the passages are copied verbatim below
            logger.warning(f"Repair call failed for {block.target_section_ids}: {exc}")
            output = RewriteOutput()
        kept, still = drafter.evaluate(block, run, output)
        drafter.keep(block, run, kept)
        if still:
            drafter.copy_failed(run, still, why="the repair failed its checks")
        run.llm_used = result.llm_used = True

    for (slot_id, unit_id) in affected:
        result.redrafted.setdefault(slot_id, []).append(unit_id)
    built = drafter.build_drafts(run, versions, origin=DraftOrigin.REPAIR, model=model if run.llm_used else None)
    result.drafts = [d for d in built if d.target_section_id in sections]
    return result

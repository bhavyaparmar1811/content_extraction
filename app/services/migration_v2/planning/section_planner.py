"""Level 1 section planner (Phase 7): rules propose, one compact LLM call confirms or corrects.

1. ``propose``: every source section gets a target from its name and content
   (``signals.py``). Top-level sections are decided name-first; a subsection
   inherits its parent's target unless its content clearly belongs elsewhere
   (a split). Cover page and table of contents are omitted; targets with no
   source become explicit gaps.
2. ``confirm``: one LLM call sees a compact outline (one line per section,
   with the proposal) and unit previews only for the sections marked [CHECK].
   It returns corrections only. Invalid corrections get one repair retry.
3. ``build_plan``: unit-level assignments become ``SectionMapping``s
   (one_to_one, merge, split with unit_ids, omit, unresolved, gaps).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.schemas.v2 import (
    GwpRule,
    MappingOrigin,
    MappingStatus,
    MappingType,
    PlanOrigin,
    SectionMapping,
    SectionPlan,
    SourceDocument,
    SourceSection,
    TemplateModel,
)
from app.services.llm.prompts.v2.section_planner import (
    PROMPT_VERSION,
    SECTION_PLANNER_SYSTEM_PROMPT,
    SECTION_PLANNER_USER_PROMPT,
)

from .signals import SectionSignal, SignalBuilder

AUTO_SPLIT_MARGIN = 0.45   # a subsection's content beats its parent's target by this much: rules split it
CHECK_SPLIT_MARGIN = 0.30  # ... by this much: ask the LLM, with unit previews
LOW_CONFIDENCE = 0.25      # top-level margin below this: ask the LLM, with unit previews
MIN_TARGET_SCORE = 0.30    # below this nothing matches: unresolved
MAX_PREVIEWS_PER_SECTION = 12
MAX_PREVIEWS = 60

_TOC = re.compile(r"^\s*(?:table\s+of\s+contents?|contents)\s*$", re.I)


# ── LLM output schema ─────────────────────────────────────────────────


class SectionChange(BaseModel):
    source_section_ids: list[str] = Field(description="Source sections this correction applies to")
    target_key: Optional[str] = Field(default=None, description="Template section key; null for omit/unresolved")
    mapping_type: MappingType
    unit_ids: list[str] = Field(default_factory=list, description="Only when part of a section moves")
    reason: str = Field(description="One short sentence")


class SectionFlag(BaseModel):
    source_section_ids: list[str] = Field(description="Sections that may contain passages belonging elsewhere")
    note: str = Field(description="One short sentence for the reviewer: what looks misplaced and where it may belong")


class SectionPlanCorrections(BaseModel):
    changes: list[SectionChange] = Field(default_factory=list)
    flags: list[SectionFlag] = Field(
        default_factory=list, description="Doubts about sections you saw no passages of; used instead of guessing unit IDs"
    )


# ── Decisions ─────────────────────────────────────────────────────────


@dataclass
class Decision:
    target_id: Optional[str]          # None: omit or unresolved
    kind: str                         # "mapped" | "omit" | "unresolved"
    reason: str
    confidence: float
    origin: MappingOrigin = MappingOrigin.RULE
    check: Optional[str] = None       # the matcher's doubt, shown to the LLM as [CHECK]
    inherited: bool = False
    moved_from: Optional[str] = None  # set when the rules moved a subsection away from its parent's target


@dataclass
class Proposal:
    source: SourceDocument
    template: TemplateModel
    decisions: dict[str, Decision]
    signals: dict[str, SectionSignal]
    unit_targets: dict[str, Optional[str]] = field(default_factory=dict)  # unit-level overrides (LLM splits)
    unit_origin: dict[str, MappingOrigin] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)                         # dropped LLM changes etc.

    @property
    def key_to_id(self) -> dict[str, str]:
        return {t.key: t.section_id for t in self.template.sections if t.key}

    @property
    def id_to_key(self) -> dict[str, str]:
        return {t.section_id: (t.key or t.section_id) for t in self.template.sections}


class SectionPlanner:
    def __init__(self, source: SourceDocument, template: TemplateModel, skip_preamble: bool = True):
        self.source = source
        self.template = template
        self.skip_preamble = skip_preamble
        self.sb = SignalBuilder(source, template)
        self.by_id = self.sb.by_id

    # ── 1. Rule proposal ──────────────────────────────────────────────

    def _roots(self) -> list[SourceSection]:
        return [s for s in self.source.sections if s.parent_id is None or s.parent_id not in self.by_id]

    def propose(self) -> Proposal:
        decisions: dict[str, Decision] = {}
        signals: dict[str, SectionSignal] = {}
        id_to_key = {t.section_id: (t.key or t.section_id) for t in self.template.sections}

        roots = self._roots()
        first_chapter = next(
            (r.section_order for r in roots if r.number and r.number != "0" and r.number.split(".")[0].isdigit()), None
        )
        for root in roots:
            subtree = self.sb.subtree(root)
            units = [u for s in subtree for u in s.units]
            all_boilerplate = bool(units) and all(u.is_boilerplate for u in units)
            sig = self.sb.signal(root)
            signals[root.section_id] = sig

            if _TOC.match(root.heading):
                decision = Decision(None, "omit", "table of contents: regenerated from the template", 1.0)
            elif root.number == "0" and self.skip_preamble:
                decision = Decision(None, "omit", "cover page: the template's cover is filled from document metadata", 1.0)
            elif self.skip_preamble and first_chapter is not None and root.section_order < first_chapter:
                # PDFs split the cover into unnumbered pseudo-sections ("GENERAL INFORMATION", "Table of Content").
                decision = Decision(None, "omit", "front matter before the first chapter (cover metadata, contents)", 1.0)
            else:
                target, score, margin = sig.best(sig.combined())
                name_t, name_s, _ = sig.best(sig.name)
                if target is None or score < MIN_TARGET_SCORE:
                    decision = Decision(None, "unresolved", "no template section matches its name or content", 0.0,
                                        check="no confident match")
                elif all_boilerplate and name_s < 0.8:
                    decision = Decision(None, "omit", "administrative content (approvals, history) with no matching section", 0.9)
                else:
                    alias = sig.name_alias.get(target)
                    why = [f"name '{alias}'" if alias and sig.name.get(target, 0) >= 0.5 else None,
                           f"content {sig.profile_text()}" if sig.profile else None]
                    decision = Decision(target, "mapped", "; ".join(w for w in why if w) or "best match",
                                        round(min(1.0, margin + 0.3), 2))
                    if margin < LOW_CONFIDENCE:
                        decision.check = (f"close call: {id_to_key[target]} vs "
                                          f"{id_to_key[sorted(sig.combined(), key=sig.combined().get)[-2]]}")
            decisions[root.section_id] = decision
            self._propose_children(root, decision, decisions, signals, id_to_key)
            moved = [s for s in subtree[1:] if decisions[s.section_id].target_id != decision.target_id]
            if moved and decision.kind == "mapped":
                # Describe the root by what stays, not by the subsections that moved out.
                kept = [u for s in subtree if s not in moved for u in s.units]
                remaining = self.sb.signal(root, kept)
                decision.reason = re.sub(r"content [^;]*", f"content {remaining.profile_text()}", decision.reason)
                signals[root.section_id] = remaining
        return Proposal(self.source, self.template, decisions, signals)

    def _propose_children(self, parent: SourceSection, parent_decision: Decision, decisions, signals, id_to_key) -> None:
        for child in self.sb.children.get(parent.section_id, []):
            sig = self.sb.signal(child)
            signals[child.section_id] = sig
            decision = Decision(parent_decision.target_id, parent_decision.kind, "inherits its parent",
                                parent_decision.confidence, inherited=True)
            if _TOC.match(child.heading):
                decision = Decision(None, "omit", "table of contents: regenerated from the template", 1.0)
            elif parent_decision.kind == "mapped" and sig.n_units >= 2 and sig.content:
                alt, alt_score, _ = sig.best(sig.content)
                here = sig.content.get(parent_decision.target_id, 0.0)
                gap = alt_score - here
                if alt and alt != parent_decision.target_id:
                    if gap >= AUTO_SPLIT_MARGIN and sig.n_units >= 3:
                        decision = Decision(alt, "mapped",
                                            f"content belongs to {id_to_key[alt]} ({sig.profile_text()}), not to its parent's "
                                            f"{id_to_key[parent_decision.target_id]}", round(min(1.0, gap + 0.3), 2),
                                            moved_from=parent_decision.target_id)
                    elif gap >= CHECK_SPLIT_MARGIN:
                        decision.check = f"content suggests {id_to_key[alt]} ({sig.profile_text()})"
                        decision.confidence = round(min(decision.confidence, 0.5), 2)
            decisions[child.section_id] = decision
            self._propose_children(child, decision, decisions, signals, id_to_key)

    # ── 2. LLM prompt ─────────────────────────────────────────────────

    def prompt(self, proposal: Proposal, preview_chars: int = 100, str_rules: Optional[list[GwpRule]] = None) -> str:
        id_to_key = proposal.id_to_key
        targets = []
        for t in self.template.sections:
            hint = _hint(t)
            types = ", ".join(sorted({s.content_type.value for s in t.slots}))
            targets.append(f"{t.key or t.section_id} | {t.heading} | {'required' if t.required else 'optional'} | {types}"
                           + (f": {hint}" if hint else ""))

        outline, check_sections = [], []
        for root in self._roots():
            self._outline(root, 0, proposal, outline, check_sections, id_to_key)

        previews = []
        for section_id in check_sections:
            units = [u for u in self.sb.subtree_units(self.by_id[section_id]) if not u.is_boilerplate and u.text.strip()]
            for u in units[:MAX_PREVIEWS_PER_SECTION]:
                if len(previews) >= MAX_PREVIEWS:
                    break
                text = re.sub(r"\s+", " ", u.text).strip()
                previews.append(f"{u.unit_id}: {text[:preview_chars]}{'…' if len(text) > preview_chars else ''}")
        preview_block = ("\nUNIT PREVIEWS (for [CHECK] and [MOVED] lines)\n" + "\n".join(previews) + "\n") if previews else ""
        rule_block = ""
        if str_rules:
            rule_block = "\nSTRUCTURAL WRITING RULES (from the GWP)\n" + "\n".join(f"{r.rule_id}: {r.text}" for r in str_rules) + "\n"
        return SECTION_PLANNER_USER_PROMPT.format(
            targets="\n".join(targets), outline="\n".join(outline), previews=preview_block, rules=rule_block
        )

    def _outline(self, section: SourceSection, depth: int, proposal: Proposal, lines: list, checks: list, id_to_key) -> None:
        decision = proposal.decisions[section.section_id]
        sig = proposal.signals[section.section_id]
        if decision.kind == "mapped":
            target = id_to_key[decision.target_id]
            proposed = "(same as parent)" if decision.inherited else f"→ {target}"
        else:
            proposed = f"→ {decision.kind} ({decision.reason})"
        mark = f" [CHECK: {decision.check}]" if decision.check else ""
        if decision.moved_from:
            mark += f" [MOVED away from parent's {id_to_key[decision.moved_from]}]"
        if decision.check or decision.moved_from:
            checks.append(section.section_id)
        heading = re.sub(r"\s+", " ", section.heading).strip()
        lines.append(f"{'  ' * depth}{section.section_id} | {heading} | {sig.n_units}u | {sig.profile_text()} | {proposed}{mark}")
        if depth >= 1:
            return  # deeper subsections are summarized in their level-2 ancestor
        for child in self.sb.children.get(section.section_id, []):
            self._outline(child, depth + 1, proposal, lines, checks, id_to_key)

    # ── 2b. Apply corrections ─────────────────────────────────────────

    def change_problems(self, proposal: Proposal, corrections: SectionPlanCorrections) -> list[str]:
        problems = []
        keys = proposal.key_to_id
        for i, change in enumerate(corrections.changes):
            where = f"changes[{i}]"
            unknown = [s for s in change.source_section_ids if s not in self.by_id]
            if not change.source_section_ids:
                problems.append(f"{where}: no source_section_ids")
            if unknown:
                problems.append(f"{where}: unknown source sections {unknown}")
            if change.mapping_type == MappingType.SPLIT and not change.unit_ids:
                problems.append(f"{where}: a split must list the unit_ids that move; for a whole section use 'move'")
            if change.mapping_type in (MappingType.OMIT, MappingType.UNRESOLVED):
                if change.unit_ids:
                    problems.append(f"{where}: omit/unresolved applies to whole sections, not unit_ids")
            elif change.target_key not in keys:
                problems.append(f"{where}: unknown target_key {change.target_key!r}; use one of {sorted(keys)}")
            for section_id in change.source_section_ids if not change.unit_ids else []:
                decision = proposal.decisions.get(section_id)
                sig = proposal.signals.get(section_id)
                if (decision is not None and sig is not None and decision.kind == "mapped" and not decision.check
                        and sig.name.get(decision.target_id, 0.0) >= 1.0
                        and (keys.get(change.target_key or "") != decision.target_id
                             or change.mapping_type in (MappingType.OMIT, MappingType.UNRESOLVED))):
                    problems.append(
                        f"{where}: {section_id} '{self.by_id[section_id].heading}' matches template section "
                        f"{proposal.id_to_key[decision.target_id]} by name, so the whole section stays there; "
                        "move single units with unit_ids if some of them belong elsewhere"
                    )
            for section_id in change.source_section_ids if not change.unit_ids else []:
                root = self._named_root(section_id, proposal)
                decision = proposal.decisions.get(section_id)
                if (root is not None and decision is not None and not decision.check and not decision.moved_from
                        and keys.get(change.target_key or "") != proposal.decisions[root].target_id):
                    problems.append(
                        f"{where}: {section_id} '{self.by_id[section_id].heading}' is under "
                        f"'{self.by_id[root].heading}', which matches {proposal.id_to_key[proposal.decisions[root].target_id]} "
                        "by name, and the matcher had no doubt about it; flag it instead, or move single units with unit_ids"
                    )
            if change.unit_ids:
                own = {u.unit_id for s in change.source_section_ids if s in self.by_id for u in self.sb.subtree_units(self.by_id[s])}
                stray = [u for u in change.unit_ids if u not in own]
                if stray:
                    problems.append(f"{where}: unit_ids {stray} are not in the listed sections")
        return problems

    def _named_root(self, section_id: str, proposal: Proposal) -> Optional[str]:
        """The top-level ancestor of a subsection when that ancestor matches its target by name; else None."""
        node = self.by_id.get(section_id)
        if node is None or not node.parent_id:
            return None
        while node.parent_id and node.parent_id in self.by_id:
            node = self.by_id[node.parent_id]
        decision, sig = proposal.decisions.get(node.section_id), proposal.signals.get(node.section_id)
        if decision is None or sig is None or decision.kind != "mapped" or sig.name.get(decision.target_id, 0.0) < 1.0:
            return None
        return node.section_id

    def is_noop(self, proposal: Proposal, change: SectionChange) -> bool:
        """A 'change' that restates the proposal (the same target for whole sections)."""
        if change.unit_ids or change.mapping_type in (MappingType.OMIT, MappingType.UNRESOLVED):
            return False
        target = proposal.key_to_id.get(change.target_key or "")
        for section_id in change.source_section_ids:
            decision = proposal.decisions.get(section_id)
            if decision is None or decision.kind != "mapped" or decision.target_id != target:
                return False
        return True

    def apply(self, proposal: Proposal, corrections: SectionPlanCorrections) -> None:
        keys = proposal.key_to_id
        changes = [c for c in corrections.changes if not self.is_noop(proposal, c)]
        named = {s for c in changes for s in c.source_section_ids}
        for change in changes:
            target = keys.get(change.target_key) if change.target_key else None
            if change.unit_ids:
                for unit_id in change.unit_ids:
                    proposal.unit_targets[unit_id] = target
                    proposal.unit_origin[unit_id] = MappingOrigin.LLM
                continue
            kind = ("omit" if change.mapping_type == MappingType.OMIT
                    else "unresolved" if change.mapping_type == MappingType.UNRESOLVED or target is None
                    else "mapped")
            for section_id in change.source_section_ids:
                for s in self.sb.subtree(self.by_id[section_id]):
                    if s.section_id != section_id and s.section_id in named:
                        continue  # the LLM decided that subsection separately
                    proposal.decisions[s.section_id] = Decision(
                        target if kind == "mapped" else None, kind, f"LLM: {change.reason}", 0.8, MappingOrigin.LLM,
                        inherited=s.section_id != section_id,
                    )
        # Every remaining doubt was seen by the LLM and left as proposed: confirmed.
        for decision in proposal.decisions.values():
            if decision.check and decision.origin == MappingOrigin.RULE:
                decision.reason += f"; confirmed by LLM (was: {decision.check})"
                decision.check = None
                decision.confidence = max(decision.confidence, 0.7)
        # The LLM's own doubts about sections it saw no passages of: for the reviewer, not applied.
        for flag in corrections.flags:
            for section_id in flag.source_section_ids:
                decision = proposal.decisions.get(section_id)
                if decision is None:
                    proposal.notes.append(f"flag on unknown section {section_id!r} dropped")
                    continue
                note = re.sub(r"\s+", " ", flag.note).strip()[:200]
                decision.check = f"LLM: {note}"

    # ── 3. Build the plan ─────────────────────────────────────────────

    def build_plan(self, proposal: Proposal, job_id: str, version: int, origin: PlanOrigin = PlanOrigin.RULE,
                   unconfirmed_review: bool = True) -> SectionPlan:
        order = {s.section_id: s.section_order for s in self.source.sections}
        root_of: dict[str, str] = {}
        for root in self._roots():
            for s in self.sb.subtree(root):
                root_of[s.section_id] = root.section_id

        # target_id -> {section_id: [unit_ids or None for "whole"]}
        placed: dict[str, dict[str, list[str]]] = {}
        partial: dict[str, set[str]] = {}  # target -> sections that are only partly there
        contributors: dict[str, list[Decision]] = {}
        llm_touched: dict[str, bool] = {}
        omitted: list[tuple[SourceSection, Decision]] = []
        unresolved: list[tuple[SourceSection, Decision]] = []

        for section in self.source.sections:
            decision = proposal.decisions.get(section.section_id)
            if decision is None:
                continue
            units = section.units
            unit_target = {u.unit_id: proposal.unit_targets.get(u.unit_id, decision.target_id) for u in units}
            if decision.kind != "mapped" and not any(u in proposal.unit_targets for u in unit_target):
                (omitted if decision.kind == "omit" else unresolved).append((section, decision))
                continue
            targets_here = {t for t in unit_target.values()} or {decision.target_id}
            for target in targets_here:
                if target is None:
                    continue
                ids = [u for u, t in unit_target.items() if t == target]
                placed.setdefault(target, {})[section.section_id] = ids
                if units and len(ids) < len(units):
                    partial.setdefault(target, set()).add(section.section_id)
                contributors.setdefault(target, []).append(decision)
                llm_touched[target] = llm_touched.get(target, False) or decision.origin == MappingOrigin.LLM or any(
                    proposal.unit_origin.get(u) == MappingOrigin.LLM for u in ids
                )

        # A root whose subtree feeds more than one target is split.
        targets_of_root: dict[str, set[str]] = {}
        for target, sections in placed.items():
            for section_id in sections:
                targets_of_root.setdefault(root_of[section_id], set()).add(target)
        split_roots = {r for r, ts in targets_of_root.items() if len(ts) > 1}

        mappings: list[SectionMapping] = []
        for t in self.template.sections:
            sections = placed.get(t.section_id)
            if not sections:
                status = MappingStatus.SOURCE_CONTENT_NOT_FOUND if t.required else MappingStatus.NOT_APPLICABLE
                mappings.append(SectionMapping(
                    target_section_id=t.section_id, mapping_type=MappingType.UNRESOLVED, status=status,
                    reason="no source content for this section" + ("" if t.required else "; optional section is removed"),
                    confidence=1.0,
                ))
                continue
            section_ids = sorted(sections, key=order.get)
            roots = list(dict.fromkeys(root_of[s] for s in section_ids))
            is_split = any(r in split_roots for r in roots) or bool(partial.get(t.section_id))
            unit_ids: list[str] = []
            if is_split:
                for s in section_ids:
                    if root_of[s] in split_roots or s in partial.get(t.section_id, set()):
                        unit_ids += sections[s]
            mapping_type = MappingType.SPLIT if is_split and unit_ids else (MappingType.MERGE if len(roots) > 1 else MappingType.ONE_TO_ONE)
            decisions = contributors[t.section_id]
            needs_review = unconfirmed_review and any(d.check for d in decisions)
            reasons = list(dict.fromkeys(d.reason for d in decisions if not d.inherited))
            mappings.append(SectionMapping(
                target_section_id=t.section_id,
                source_section_ids=section_ids,
                unit_ids=unit_ids,
                mapping_type=mapping_type,
                status=MappingStatus.NEEDS_REVIEW if needs_review else MappingStatus.MAPPED,
                reason=_clip("; ".join(reasons) + "".join(f"; CHECK {d.check}" for d in decisions if d.check)),
                confidence=round(min(d.confidence for d in decisions), 2),
                origin=MappingOrigin.LLM if llm_touched.get(t.section_id) else MappingOrigin.RULE,
            ))

        for kind, items in (("omit", omitted), ("unresolved", unresolved)):
            by_root: dict[str, list[tuple[SourceSection, Decision]]] = {}
            for section, decision in items:
                by_root.setdefault(root_of[section.section_id], []).append((section, decision))
            for group in by_root.values():
                ids = sorted((s.section_id for s, _ in group), key=order.get)
                decision = group[0][1]
                if kind == "omit":
                    mappings.append(SectionMapping(
                        source_section_ids=ids, mapping_type=MappingType.OMIT, status=MappingStatus.NOT_APPLICABLE,
                        reason=_clip(decision.reason), justification=decision.reason, confidence=decision.confidence,
                        origin=decision.origin,
                    ))
                else:
                    mappings.append(SectionMapping(
                        source_section_ids=ids, mapping_type=MappingType.UNRESOLVED, status=MappingStatus.NEEDS_REVIEW,
                        reason=_clip(decision.reason), confidence=decision.confidence, origin=decision.origin,
                    ))
        return SectionPlan(job_id=job_id, version=version, origin=origin, mappings=mappings)


def _hint(target) -> str:
    """First meaningful sentence of the target's instructions, ≤100 chars."""
    texts = [target.purpose_instruction or ""] + [s.instruction for s in target.slots]
    for text in texts:
        for line in re.split(r"[\n]+|(?<=[.?!])\s+", text or ""):
            line = line.strip()
            if (len(line) > 20 and not line.endswith(":") and "(None)" not in line
                    and not re.match(r"(?i)^(if\b|please|for directives)", line)):
                return line[:100]
    return ""


def _clip(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── LLM call ──────────────────────────────────────────────────────────


@dataclass
class LLMResult:
    corrections: SectionPlanCorrections
    usage: dict[str, int]
    problems: list[str]


def _usage(raw: Any) -> dict[str, int]:
    meta = getattr(raw, "usage_metadata", None) or {}
    return {k: int(meta.get(k, 0)) for k in ("input_tokens", "output_tokens", "total_tokens") if k in meta}


async def run_confirm(planner: SectionPlanner, proposal: Proposal, chain: Any, prompt: str, rate_limiter: Any = None) -> LLMResult:
    """One call, plus one repair retry if the corrections are invalid. Invalid changes left after that are dropped."""
    messages = [SystemMessage(content=SECTION_PLANNER_SYSTEM_PROMPT), HumanMessage(content=prompt)]
    usage: dict[str, int] = {"calls": 0}

    async def call(msgs):
        coro = lambda: chain.ainvoke(msgs)  # noqa: E731
        result = await (rate_limiter.execute(coro, task_name="section_planner") if rate_limiter else coro())
        usage["calls"] += 1
        if isinstance(result, dict) and "parsed" in result:
            for k, v in _usage(result.get("raw")).items():
                usage[k] = usage.get(k, 0) + v
            if result.get("parsed") is None:
                raise ValueError(f"unparseable planner output: {result.get('parsing_error')}")
            parsed = result["parsed"]
        else:
            parsed = result
        return parsed if isinstance(parsed, SectionPlanCorrections) else SectionPlanCorrections.model_validate(parsed)

    corrections = await call(messages)
    problems = planner.change_problems(proposal, corrections)
    # A repair costs a second call: only worth it when an invalid change concerns one of the matcher's doubts.
    doubtful = {sid for sid, d in proposal.decisions.items() if d.check or d.moved_from}
    bad = _bad_indexes(problems)
    if problems and not any(set(corrections.changes[i].source_section_ids) & doubtful for i in bad if i < len(corrections.changes)):
        logger.info(f"Section planner: dropping {len(problems)} invalid correction(s) on confident sections")
    elif problems:
        logger.info(f"Section planner: {len(problems)} invalid correction(s), asking once for a repair")
        repair = HumanMessage(content=(
            "Your corrections had problems:\n- " + "\n- ".join(problems)
            + "\n\nYour previous corrections were:\n" + json.dumps(corrections.model_dump(mode="json"))
            + "\n\nReturn the full corrected list of changes."
        ))
        corrections = await call(messages + [repair])
        problems = planner.change_problems(proposal, corrections)
    if problems:
        bad = _bad_indexes(problems)
        # A move dropped only because it named passages the model never saw still carries a hunch:
        # keep it for the reviewer as a flag on the (real) sections it named.
        # The same for a whole-subsection move out of a chapter its heading places (the matcher had no doubt).
        hunch = re.compile(r"changes\[(\d+)\]: (?:unit_ids .* not in the listed|\S+ '.*' is under ')")
        guessed = {int(m.group(1)) for p in problems if (m := hunch.match(p))}
        flags = list(corrections.flags)
        already = {s for f in flags for s in f.source_section_ids}
        for i in sorted(guessed):
            if i >= len(corrections.changes) or any(p.startswith(f"changes[{i}]:") and not hunch.match(p) for p in problems):
                continue
            change = corrections.changes[i]
            sections = [s for s in change.source_section_ids if s in planner.by_id]
            if not sections or already & set(sections):
                continue  # nothing real to point at, or the model flagged it itself
            target = proposal.key_to_id.get(change.target_key or "")
            current = {proposal.decisions[s].target_id for s in sections if s in proposal.decisions}
            where = f" to {change.target_key}" if target and current != {target} else " elsewhere"
            flags.append(SectionFlag(source_section_ids=sections, note=f"may hold passages that belong{where}: {change.reason}"))
            already |= set(sections)
        corrections = corrections.model_copy(update={
            "changes": [c for i, c in enumerate(corrections.changes) if i not in bad], "flags": flags,
        })
    return LLMResult(corrections, usage, problems)


def _bad_indexes(problems: list[str]) -> set[int]:
    return {int(m.group(1)) for p in problems if (m := re.match(r"changes\[(\d+)\]", p))}

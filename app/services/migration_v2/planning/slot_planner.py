"""Level 2 slot planner (Phase 8): rules place passages into slots, one compact LLM call per block confirms or corrects.

Runs inside each approved section mapping: a passage never leaves the target
section the section plan gave it.

1. ``propose``: every in-scope passage gets slots (``slot_signals.py``).
   - A section with one content slot takes everything.
   - Table-only sections place whole tables by header and cells (terms vs
     abbreviations, roles vs RACI); text lines go with the table they introduce.
   - Other sections use icon rows (by position, when the counts match) and cue
     words from each slot's own instruction. A passage that states several
     things (roles, units and geography in one sentence) feeds several slots.
   - A source lead-in that answers a template inline choice ("This SOP is
     applicable:") becomes a ``RegionChoice``, not slot content.
   - Callout rule pass: a source warning box → ``attention``, a note → ``explanation``.
2. ``confirm``: per token-bounded block, the LLM sees the slots, the callout
   palette, the job's GWP structural and formatting rules, and one line per
   passage with its proposed slots. It resolves the [CHECK] lines, proposes
   callouts (palette kinds only) and flags doubts. Invalid items get one repair
   retry, then are dropped.
3. ``build_plan``: a ``SlotPlan`` with, per slot, the ordered units, the
   migration action and the GWP rule IDs the drafter will apply. Empty required
   slots are ``source_content_not_found`` (a gap for the reviewer); empty
   optional slots are ``not_applicable`` (removed).

Without a GWP (placement mode) every mapped slot is ``copy_verbatim``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.schemas.v2 import (
    AssignmentOrigin,
    CalloutAssignment,
    CalloutKind,
    ContentType,
    GwpRule,
    GwpRuleSet,
    MappingOrigin,
    MappingStatus,
    MigrationAction,
    PlanOrigin,
    RegionChoice,
    RuleCategory,
    SectionMapping,
    SectionPlan,
    SectionSlotPlan,
    SlotMapping,
    SlotPlan,
    SourceDocument,
    SourceUnit,
    TargetSection,
    TargetSlot,
    TemplateModel,
    UnitType,
)
from app.services.llm.prompts.v2.slot_planner import (
    PROMPT_VERSION,
    SLOT_PLANNER_SYSTEM_PROMPT,
    SLOT_PLANNER_USER_PROMPT,
)

from .budget import Block, Line, SectionLines, pack
from .section_validator import covered_units
from .slot_signals import (
    icon_groups,
    icon_slots,
    inline_choice,
    is_callout_slot,
    is_table_row,
    is_table_slot,
    is_text_unit,
    lexical_best,
    table_scores,
    text_scores,
    has_icon,
)

LONG_TEXT_IN_TABLE_SLOT = 300  # chars: longer narrative text in a table-only section is checked
MIN_LEXICAL = 0.08             # instruction similarity below this is no evidence at all
MAX_CALLOUT_UNITS = 8
DEFAULT_BLOCK_TOKENS = 5000
DEFAULT_PREVIEW_CHARS = 160

_LIST_TYPES = (UnitType.BULLET, UnitType.PROCEDURE_STEP)
_TYPE_ABBR = {
    UnitType.PARAGRAPH: "p", UnitType.BULLET: "bullet", UnitType.PROCEDURE_STEP: "step", UnitType.NOTE: "note",
    UnitType.WARNING: "warning", UnitType.CAPTION: "caption", UnitType.FIGURE: "figure",
    UnitType.HEADING_STATEMENT: "heading", UnitType.DEFINITION: "definition", UnitType.REFERENCE: "reference",
}


# ── LLM output schema ─────────────────────────────────────────────────


class SlotChange(BaseModel):
    unit_ids: list[str] = Field(description="Passages whose slots change; all from one section")
    slot_refs: list[str] = Field(default_factory=list, description="'SECTION.slot' refs; empty when the passage fits no slot")
    reason: str = Field(description="One short sentence")


class CalloutProposal(BaseModel):
    unit_ids: list[str] = Field(description="Consecutive passages of one section")
    kind: CalloutKind
    reason: str = Field(description="One short sentence")


class SlotFlag(BaseModel):
    unit_ids: list[str]
    note: str = Field(description="One short sentence for the reviewer")


class SlotPlanCorrections(BaseModel):
    changes: list[SlotChange] = Field(default_factory=list)
    callouts: list[CalloutProposal] = Field(default_factory=list)
    flags: list[SlotFlag] = Field(default_factory=list)


# ── Working state ─────────────────────────────────────────────────────


@dataclass
class Placement:
    slot_ids: list[str]               # empty: fits no slot (unplaced, for the reviewer)
    reason: str
    origin: MappingOrigin = MappingOrigin.RULE
    check: Optional[str] = None       # the matcher's doubt, shown to the LLM as [CHECK]


@dataclass
class SectionWork:
    target: TargetSection
    mappings: list[SectionMapping]
    units: list[SourceUnit]           # in scope, source order
    placements: dict[str, Placement] = field(default_factory=dict)
    regions: list[RegionChoice] = field(default_factory=list)
    callouts: list[CalloutAssignment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def section_status(self) -> Optional[MappingStatus]:
        return self.mappings[0].status if self.mappings else None

    def slot(self, slot_id: str) -> Optional[TargetSlot]:
        return next((s for s in self.target.slots if s.slot_id == slot_id), None)

    def ref(self, slot_id: str) -> str:
        slot = self.slot(slot_id)
        return f"{self.target.key or self.target.section_id}.{slot.key if slot and slot.key else slot_id}"

    def slot_by_ref(self, ref: str) -> Optional[TargetSlot]:
        section_key, _, slot_key = (ref or "").strip().partition(".")
        if section_key not in (self.target.key, self.target.section_id):
            return None
        return next((s for s in self.target.slots if slot_key in (s.key, s.slot_id) and not is_callout_slot(s)), None)

    def callout_of(self, unit_id: str) -> Optional[CalloutKind]:
        return next((c.kind for c in self.callouts if unit_id in c.unit_ids), None)


@dataclass
class SlotProposal:
    sections: list[SectionWork]
    notes: list[str] = field(default_factory=list)  # dropped LLM items

    def work(self, target_section_id: str) -> Optional[SectionWork]:
        return next((w for w in self.sections if w.target.section_id == target_section_id), None)

    def work_of_unit(self, unit_id: str) -> Optional[SectionWork]:
        return next((w for w in self.sections if unit_id in w.placements), None)


def _clip(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SlotPlanner:
    def __init__(self, source: SourceDocument, template: TemplateModel, section_plan: SectionPlan,
                 rules: Optional[GwpRuleSet] = None):
        self.source = source
        self.template = template
        self.section_plan = section_plan
        self.rules = rules
        self.unit_by_id = {u.unit_id: u for u in source.iter_units()}
        self.section_units = {s.section_id: [u.unit_id for u in s.units] for s in source.sections}
        self.palette = {c.kind: c for c in template.callout_palette}

    # ── 1. Rule proposal ──────────────────────────────────────────────

    def scope(self, target: TargetSection) -> tuple[list[SectionMapping], list[SourceUnit]]:
        mappings = [m for m in self.section_plan.mappings if m.target_section_id == target.section_id]
        ids: list[str] = []
        for m in mappings:
            if m.status in (MappingStatus.MAPPED, MappingStatus.NEEDS_REVIEW) and m.source_section_ids:
                ids += covered_units(m, self.section_units)
        units = sorted((self.unit_by_id[i] for i in dict.fromkeys(ids) if i in self.unit_by_id), key=lambda u: u.seq)
        substantive = [u for u in units if not u.is_boilerplate and (u.text.strip() or u.unit_type == UnitType.FIGURE)]
        if not substantive:  # e.g. document history rows: administrative, but the section's whole content
            substantive = [u for u in units if u.text.strip()]
        return mappings, substantive

    def propose(self) -> SlotProposal:
        sections = []
        for target in self.template.sections:
            mappings, units = self.scope(target)
            work = SectionWork(target, mappings, units)
            if units:
                self._place(work)
                self._rule_callouts(work)
            sections.append(work)
        return SlotProposal(sections)

    def _place(self, work: SectionWork) -> None:
        remaining = []
        for unit in work.units:
            hit = inline_choice(unit, work.target.conditional_regions) if is_text_unit(unit) else None
            if hit:
                region, choice = hit
                work.regions.append(RegionChoice(region_id=region.region_id, unit_ids=[unit.unit_id], choice=choice))
            else:
                remaining.append(unit)
        slots = sorted((s for s in work.target.slots if not is_callout_slot(s)), key=lambda s: s.display_order)
        if not slots:
            for unit in remaining:
                work.placements[unit.unit_id] = Placement([], "the section has no content slot",
                                                          check="no content slot in this section")
        elif len(slots) == 1:
            for unit in remaining:
                work.placements[unit.unit_id] = Placement([slots[0].slot_id], "the section's only content slot")
        elif all(is_table_slot(s) for s in slots):
            self._place_tables(work, remaining, slots)
        else:
            self._place_text(work, remaining, slots)

        support = next((s for s in slots if s.content_type == ContentType.SUPPORTING_INFORMATION), None)
        if support:
            for placement in work.placements.values():
                if not placement.slot_ids:
                    placement.slot_ids = [support.slot_id]
                    placement.reason += "; fits no other slot: supporting information"

    @staticmethod
    def _items(units: list[SourceUnit]) -> list[list[SourceUnit]]:
        """Consecutive rows of one table form one item; every other unit is its own item."""
        items: list[list[SourceUnit]] = []
        for unit in units:
            table = unit.table_ref.table_id if is_table_row(unit) else None
            if table and items and is_table_row(items[-1][0]) and items[-1][0].table_ref.table_id == table:
                items[-1].append(unit)
            else:
                items.append([unit])
        return items

    def _table_slot(self, rows: list[SourceUnit], slots: list[TargetSlot], previous: Optional[str]) -> tuple[str, str, Optional[str]]:
        scores = table_scores(rows, slots)
        best_id, best = max(scores.items(), key=lambda kv: kv[1]) if scores else (None, 0.0)
        headers = " | ".join(rows[0].table_ref.header_cells) if rows[0].table_ref else ""
        if best > 0:
            ties = [s for s, v in scores.items() if v == best and s != best_id]
            check = f"table matches several slots ({', '.join(ties)})" if ties else None
            return best_id, f"table '{_clip(headers, 60)}'", check
        if previous:
            return previous, "continues the table before it (e.g. its legend)", None
        slot, score = lexical_best(" ".join([headers] + [r.text for r in rows]), slots)
        if slot and score >= MIN_LEXICAL:
            return slot.slot_id, f"table '{_clip(headers, 60)}' (closest instruction)", "no table cue"
        return slots[0].slot_id, f"table '{_clip(headers, 60)}'", "no table cue matched any slot"

    def _place_tables(self, work: SectionWork, units: list[SourceUnit], slots: list[TargetSlot]) -> None:
        items = self._items(units)
        table_slot: dict[int, str] = {}
        previous = None
        for i, item in enumerate(items):
            if not is_table_row(item[0]):
                continue
            slot_id, reason, check = self._table_slot(item, slots, previous)
            table_slot[i] = previous = slot_id
            for row in item:
                work.placements[row.unit_id] = Placement([slot_id], reason, check=check)

        for i, item in enumerate(items):
            if i in table_slot:
                continue
            unit = item[0]
            if unit.unit_type in (UnitType.FIGURE, UnitType.CAPTION):
                work.placements[unit.unit_id] = Placement(
                    [], "figure in a section of tables",
                    check="figure in a section of tables: keep it beside a table, move it, or drop it")
                continue
            before = next((table_slot[j] for j in range(i - 1, -1, -1) if j in table_slot), None)
            after = next((table_slot[j] for j in range(i + 1, len(items)) if j in table_slot), None)
            text = unit.text.strip()
            intro = text.endswith(":") or bool(re.search(r"\b(?:following|below)\b[^.]{0,40}\btables?\b|\btables?\s+below\b", text, re.I))
            slot_id = (after if intro and after else before or after) or slots[0].slot_id
            check = None
            if len(text) > LONG_TEXT_IN_TABLE_SLOT:
                check = f"long text ({len(text)} chars) in a table slot: keep it as a note with the table, move it, or make it a callout"
            reason = "introduces the table" if intro and after else "note beside the table" if (before or after) else "text of a table section"
            work.placements[unit.unit_id] = Placement([slot_id], reason, check=check)

    def _place_text(self, work: SectionWork, units: list[SourceUnit], slots: list[TargetSlot]) -> None:
        tables = [item for item in self._items(units) if is_table_row(item[0])]
        for rows in tables:
            scores = table_scores(rows, slots)
            if max(scores.values(), default=0) > 0:
                slot_id = max(scores.items(), key=lambda kv: kv[1])[0]
                placement = Placement([slot_id], "table header")
            else:
                placement = self._by_cues(" ".join(r.text for r in rows), slots)
            for row in rows:
                work.placements[row.unit_id] = placement
        in_tables = {r.unit_id for rows in tables for r in rows}

        groups = icon_groups([u for u in units if u.unit_id not in in_tables])
        rows = icon_slots(work.target)
        icon_rows = [g for g in groups if has_icon(g[0])]
        aligned = bool(rows) and len(icon_rows) == len(rows)
        intro: Optional[Placement] = None
        last: Optional[Placement] = None
        for group in groups:
            first = group[0]
            if aligned and has_icon(first):
                k = icon_rows.index(group)
                placement = Placement([rows[k].slot_id], f"icon row {k + 1} of {len(rows)}")
            elif first.unit_type in (UnitType.FIGURE, UnitType.CAPTION):
                placement = Placement(list(last.slot_ids), "figure with the text before it") if last else Placement(
                    [], "figure", check="figure with no text before it")
            else:
                placement = self._by_cues(" ".join(u.text for u in group), slots)
                if first.unit_type in _LIST_TYPES and intro is not None and placement.check:
                    placement = Placement(list(intro.slot_ids), "list item under its intro line")
            for unit in group:
                work.placements[unit.unit_id] = placement
            last = placement
            if first.unit_type not in _LIST_TYPES:
                intro = placement if first.text.rstrip().endswith(":") else None

    def _by_cues(self, text: str, slots: list[TargetSlot]) -> Placement:
        scores = text_scores(text, slots)
        ranked = scores.ranked()
        key = {s.slot_id: s.key or s.slot_id for s in slots}
        order = {s.slot_id: s.display_order for s in slots}
        if ranked:
            best_id, best = ranked[0]
            chosen = [best_id] + [s for s, _ in ranked[1:] if scores.strong.get(s)]
            tied = [s for s, v in ranked[1:] if v == best and s not in chosen]
            check = f"as clear a match for {', '.join(key[s] for s in tied)}" if tied else None
            chosen.sort(key=order.get)
            reason = "cue words: " + ", ".join(f"{key[s]} {scores.hits[s]}" for s in chosen)
            return Placement(chosen, reason, check=check)
        slot, score = lexical_best(text, slots)
        if slot and score >= MIN_LEXICAL:
            return Placement([slot.slot_id], f"closest instruction ({score:.2f})", check="no cue word; closest instruction")
        fallback = next((s for s in slots if s.required), slots[0])
        return Placement([fallback.slot_id], "first required slot", check="no cue word matched any slot")

    def _rule_callouts(self, work: SectionWork) -> None:
        for unit in work.units:
            kind = {UnitType.WARNING: CalloutKind.ATTENTION, UnitType.NOTE: CalloutKind.EXPLANATION}.get(unit.unit_type)
            if kind and kind in self.palette and unit.unit_id in work.placements and not work.callout_of(unit.unit_id):
                work.callouts.append(CalloutAssignment(
                    unit_ids=[unit.unit_id], kind=kind, origin=AssignmentOrigin.RULE,
                    reason=f"the source shows it as a {unit.unit_type.value} box",
                ))

    # ── 2. LLM prompt ─────────────────────────────────────────────────

    def callout_sections(self) -> set[str]:
        """Sections where callout boxes may be proposed: where the template places its own boxes.

        A template with a palette but no placed boxes allows them in free-text sections (not tables, not icon rows).
        """
        placed = {t.section_id for t in self.template.sections if any(is_callout_slot(s) for s in t.slots)}
        if placed:
            return placed
        return {t.section_id for t in self.template.sections
                if any(not is_callout_slot(s) and not is_table_slot(s) and s.icon is None for s in t.slots)}

    def needs_llm(self, work: SectionWork) -> bool:
        """Sections with doubts, or with running text where a callout box may go."""
        if any(p.check for p in work.placements.values()):
            return True
        return work.target.section_id in self.callout_sections() and bool(self.palette) and any(
            is_text_unit(u) and u.unit_id in work.placements for u in work.units)

    def section_lines(self, work: SectionWork, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> SectionLines:
        sources = ", ".join(dict.fromkeys(s for m in work.mappings for s in m.source_section_ids))
        boxes = "callout boxes allowed" if work.target.section_id in self.callout_sections() else "no callout boxes"
        header = f"## {work.target.key or work.target.section_id} | {work.target.heading} (source: {sources}; {boxes})"
        lines: list[Line] = []
        for item in self._items([u for u in work.units if u.unit_id in work.placements]):
            first = item[0]
            placement = work.placements[first.unit_id]
            refs = ", ".join(work.ref(s).split(".", 1)[1] for s in placement.slot_ids) or "none"
            mark = f" [CHECK: {placement.check}]" if placement.check else ""
            kind = work.callout_of(first.unit_id)
            mark += f" [CALLOUT: {kind.value}]" if kind else ""
            if is_table_row(first) and len(item) > 1:
                headers = " | ".join(first.table_ref.header_cells)
                ids = f"{first.unit_id}..{item[-1].unit_id}"
                text = f"table, {len(item)} rows; header: {_clip(headers, 80)}; row 1: {_clip(first.text, 80)}"
                lines.append(Line(f"{ids} | table | → {refs}{mark} | {text}", [u.unit_id for u in item]))
                continue
            kind_abbr = "row" if is_table_row(first) else _TYPE_ABBR.get(first.unit_type, first.unit_type.value)
            text = _clip(first.text, preview_chars)
            lines.append(Line(f"{first.unit_id} | {kind_abbr} | → {refs}{mark} | {text}", [first.unit_id],
                              break_ok=first.unit_type not in _LIST_TYPES))
        return SectionLines(work.target.section_id, header, lines)

    def blocks(self, proposal: SlotProposal, budget: int = DEFAULT_BLOCK_TOKENS,
               preview_chars: int = DEFAULT_PREVIEW_CHARS) -> list[Block]:
        sections = [self.section_lines(w, preview_chars) for w in proposal.sections if w.placements and self.needs_llm(w)]
        return pack([s for s in sections if s.lines], budget)

    def block_rules(self, block: Block) -> list[GwpRule]:
        """The job's GWP rules on placement and layout (STR, FMT) for the content types in the block."""
        if self.rules is None:
            return []
        types = {s.content_type for sid in block.target_section_ids for t in self.template.sections
                 if t.section_id == sid for s in t.slots}
        return self.rules.select([RuleCategory.STRUCTURAL, RuleCategory.FORMATTING], sorted(types, key=lambda t: t.value))

    def prompt(self, proposal: SlotProposal, block: Block) -> str:
        slots = []
        for section_id in block.target_section_ids:
            work = proposal.work(section_id)
            for slot in sorted(work.target.slots, key=lambda s: s.display_order):
                if is_callout_slot(slot):
                    continue
                hint = _clip(slot.instruction.replace("\n", " / "), 120)
                slots.append(f"{work.ref(slot.slot_id)} | {slot.content_type.value} | "
                             f"{'required' if slot.required else 'optional'} | {hint}")
        callouts = "\n".join(f"{k.value} = {style.label}" for k, style in self.palette.items()) or "(none: do not propose callouts)"
        rules = self.block_rules(block)
        rule_block = ("\nGWP RULES (structure and formatting, from the organisation's writing guide)\n"
                      + "\n".join(f"{r.rule_id}: {r.text}" for r in rules) + "\n") if rules else ""
        return SLOT_PLANNER_USER_PROMPT.format(slots="\n".join(slots), callouts=callouts, passages=block.render(),
                                               rules=rule_block)

    # ── 2b. Check and apply corrections ───────────────────────────────

    def _one_section(self, proposal: SlotProposal, unit_ids: list[str], block: Block) -> tuple[Optional[SectionWork], list[str]]:
        if not unit_ids:
            return None, ["no unit_ids"]
        unknown = [u for u in unit_ids if u not in block.unit_ids]
        if unknown:
            return None, [f"unit_ids {unknown} are not among the passages shown"]
        if len({proposal.work_of_unit(u).target.section_id for u in unit_ids}) > 1:
            return None, ["unit_ids span several sections; a change stays inside one section"]
        return proposal.work_of_unit(unit_ids[0]), []

    def _extend_list(self, work: SectionWork, unit_ids: list[str]) -> list[str]:
        """An intro line ending in ':' takes the list right after it (items of the first item's list type)."""
        order = [u.unit_id for u in work.units if u.unit_id in work.placements]
        out = list(unit_ids)
        last = self.unit_by_id[out[-1]]
        if last.text.rstrip().endswith(":"):
            list_type = None
            for unit_id in order[order.index(last.unit_id) + 1:]:
                unit_type = self.unit_by_id[unit_id].unit_type
                if unit_type not in _LIST_TYPES or (list_type is not None and unit_type != list_type):
                    break
                list_type = unit_type
                if unit_id not in out:
                    out.append(unit_id)
        return out

    def change_problems(self, proposal: SlotProposal, block: Block, change: SlotChange) -> list[str]:
        work, problems = self._one_section(proposal, change.unit_ids, block)
        if work is None:
            return problems
        bad = [r for r in change.slot_refs if work.slot_by_ref(r) is None]
        content = [s for s in work.target.slots if not is_callout_slot(s)]
        if bad:
            return [f"slot refs {bad} are not content slots of {work.target.key}; use {[work.ref(s.slot_id) for s in content]}"]
        if not change.slot_refs and len(content) == 1 and content[0].content_type != ContentType.SUPPORTING_INFORMATION:
            return [f"{work.target.key} has one content slot, which takes every passage of the section; "
                    "a passage that belongs to another section is a section-plan matter: flag it instead"]
        return []

    def callout_problems(self, proposal: SlotProposal, block: Block, callout: CalloutProposal) -> list[str]:
        work, problems = self._one_section(proposal, callout.unit_ids, block)
        if work is None:
            return problems
        if work.target.section_id not in self.callout_sections():
            problems.append(f"{work.target.key} has no callout boxes in this template")
        if callout.kind not in self.palette:
            problems.append(f"kind {callout.kind.value!r} is not in the template palette "
                            f"({', '.join(k.value for k in self.palette)})")
        not_text = [u for u in callout.unit_ids if not is_text_unit(self.unit_by_id[u])]
        if not_text:
            problems.append(f"{not_text} are tables, figures or captions; a callout holds text only")
        order = [u.unit_id for u in work.units if u.unit_id in work.placements]
        ids = sorted(callout.unit_ids, key=order.index)
        positions = [order.index(u) for u in ids]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            problems.append("a callout lists consecutive passages")
        extended = self._extend_list(work, ids)
        taken = [u for u in extended if work.callout_of(u)]
        if taken:
            problems.append(f"{taken} are already in a callout")
        if len(extended) > MAX_CALLOUT_UNITS:
            problems.append(f"at most {MAX_CALLOUT_UNITS} passages per callout (with the list an intro line takes)")
        return problems

    def check_corrections(self, proposal: SlotProposal, block: Block, corrections: SlotPlanCorrections) -> list[str]:
        problems = [f"changes[{i}]: {p}" for i, c in enumerate(corrections.changes) for p in self.change_problems(proposal, block, c)]
        problems += [f"callouts[{i}]: {p}" for i, c in enumerate(corrections.callouts)
                     for p in self.callout_problems(proposal, block, c)]
        problems += [f"flags[{i}]: {p}" for i, f in enumerate(corrections.flags) for p in self._one_section(proposal, f.unit_ids, block)[1]]
        return problems

    def apply(self, proposal: SlotProposal, block: Block, corrections: SlotPlanCorrections) -> list[str]:
        """Apply the valid items; return what was dropped. Doubts the LLM saw and left alone count as confirmed."""
        dropped: list[str] = []
        for change in corrections.changes:
            problems = self.change_problems(proposal, block, change)
            if problems:
                dropped.append(f"change {change.unit_ids} → {change.slot_refs}: {'; '.join(problems)}")
                continue
            work = proposal.work_of_unit(change.unit_ids[0])
            ids = sorted({work.slot_by_ref(r).slot_id for r in change.slot_refs}, key=lambda sid: work.slot(sid).display_order)
            for unit_id in change.unit_ids:
                work.placements[unit_id] = Placement(
                    ids, f"LLM: {_clip(change.reason, 200)}", MappingOrigin.LLM,
                    check=None if ids else f"LLM: fits no slot ({_clip(change.reason, 150)})",
                )
        for callout in corrections.callouts:
            problems = self.callout_problems(proposal, block, callout)
            if problems:
                dropped.append(f"callout {callout.kind.value} {callout.unit_ids}: {'; '.join(problems)}")
                continue
            work = proposal.work_of_unit(callout.unit_ids[0])
            order = [u.unit_id for u in work.units if u.unit_id in work.placements]
            ids = self._extend_list(work, sorted(callout.unit_ids, key=order.index))
            work.callouts.append(CalloutAssignment(unit_ids=ids, kind=callout.kind, origin=AssignmentOrigin.LLM,
                                                   reason=_clip(callout.reason, 200)))
        seen: set[int] = set()  # a group of passages shares one Placement
        for unit_id in block.unit_ids:
            work = proposal.work_of_unit(unit_id)
            placement = work.placements[unit_id] if work else None
            if placement is None or id(placement) in seen:
                continue
            seen.add(id(placement))
            if placement.check and placement.origin == MappingOrigin.RULE:
                placement.reason += f"; confirmed by LLM (was: {placement.check})"
                placement.check = None if placement.slot_ids else "fits no slot of this section"
        for flag in corrections.flags:
            for unit_id in flag.unit_ids:
                work = proposal.work_of_unit(unit_id)
                if work and unit_id in block.unit_ids:
                    work.placements[unit_id] = replace(work.placements[unit_id], check=f"LLM: {_clip(flag.note, 200)}")
        return dropped

    # ── 3. Build the plan ─────────────────────────────────────────────

    def rule_ids_for(self, slot: TargetSlot) -> list[str]:
        """GWP rules the drafter applies to this slot: style, preservation and formatting, by content type."""
        if self.rules is None:
            return []
        cats = [RuleCategory.STYLE, RuleCategory.PRESERVATION, RuleCategory.FORMATTING]
        return [r.rule_id for r in self.rules.select(cats, [slot.content_type])]

    def action_for(self, slot: TargetSlot) -> MigrationAction:
        """Placement mode (no GWP style rule for this content) copies; otherwise the drafter rewrites in house style."""
        style = self.rules.select([RuleCategory.STYLE], [slot.content_type]) if self.rules else []
        if not style or is_table_slot(slot):
            return MigrationAction.COPY_VERBATIM
        return MigrationAction.EXTRACT_AND_REWRITE

    def _empty(self, work: SectionWork, slot: TargetSlot, filled: set[str]) -> tuple[MappingStatus, str]:
        keys = {s.slot_id: s.key for s in work.target.slots}
        if not work.units and not work.target.required:
            return MappingStatus.NOT_APPLICABLE, "optional section with no source content: removed"
        if is_callout_slot(slot):
            return MappingStatus.NOT_APPLICABLE, "no passage promoted to this callout; the box is removed"
        for group in work.target.one_of:
            if slot.key in group:
                members = [s for s in work.target.slots if s.key in group]
                if any(s.slot_id in filled for s in members):
                    return MappingStatus.NOT_APPLICABLE, f"one of {' / '.join(group)} is filled"
                first = min(members, key=lambda s: s.display_order)
                if slot.slot_id == first.slot_id:
                    return MappingStatus.SOURCE_CONTENT_NOT_FOUND, f"none of {' / '.join(group)} has source content: gap"
                return MappingStatus.NOT_APPLICABLE, f"gap reported on {keys[first.slot_id]}"
        if slot.required:
            return MappingStatus.SOURCE_CONTENT_NOT_FOUND, "no source content: gap marker for the reviewer"
        return MappingStatus.NOT_APPLICABLE, "optional slot with no source content is removed"

    def build_plan(self, proposal: SlotProposal, job_id: str, version: int, origin: PlanOrigin = PlanOrigin.RULE,
                   unconfirmed_review: bool = True) -> SlotPlan:
        sections = []
        for work in proposal.sections:
            seq = {u.unit_id: u.seq for u in work.units}
            slot_units: dict[str, list[str]] = {}
            for unit_id, placement in work.placements.items():
                for slot_id in placement.slot_ids:
                    slot_units.setdefault(slot_id, []).append(unit_id)
            fixed = {s.callout_kind: s for s in work.target.slots if is_callout_slot(s)}
            callout_origin: dict[str, MappingOrigin] = {}
            for assignment in work.callouts:
                box = fixed.get(assignment.kind)
                if box is not None:  # the template's own box of that kind takes the promoted passages
                    slot_units.setdefault(box.slot_id, []).extend(assignment.unit_ids)
                    if assignment.origin == AssignmentOrigin.LLM:
                        callout_origin[box.slot_id] = MappingOrigin.LLM
            shared = {u for u, p in work.placements.items() if len(p.slot_ids) > 1}
            filled = {s for s, ids in slot_units.items() if ids}

            mappings = []
            for slot in sorted(work.target.slots, key=lambda s: s.display_order):
                ids = sorted(dict.fromkeys(slot_units.get(slot.slot_id, [])), key=lambda u: seq.get(u, 0))
                if not ids:
                    status, note = self._empty(work, slot, filled)
                    mappings.append(SlotMapping(slot_id=slot.slot_id, status=status, migration_action=MigrationAction.NONE,
                                                note=note, requires_human_review=status == MappingStatus.SOURCE_CONTENT_NOT_FOUND))
                    continue
                placements = [work.placements[u] for u in ids if u in work.placements]
                doubts = list(dict.fromkeys(p.check for p in placements if p.check)) if unconfirmed_review else []
                origin_here = callout_origin.get(slot.slot_id) or (
                    MappingOrigin.LLM if any(p.origin == MappingOrigin.LLM for p in placements) else MappingOrigin.RULE)
                mappings.append(SlotMapping(
                    slot_id=slot.slot_id,
                    source_unit_ids=ids,
                    extraction_scope=[slot.key] if slot.key and shared.intersection(ids) else [],
                    migration_action=self.action_for(slot),
                    status=MappingStatus.NEEDS_REVIEW if doubts else MappingStatus.MAPPED,
                    requires_human_review=bool(doubts),
                    note=_clip("; ".join(doubts)) if doubts else None,
                    origin=origin_here,
                    rule_ids=self.rule_ids_for(slot),
                ))
            unplaced = [u for u, p in sorted(work.placements.items(), key=lambda kv: seq.get(kv[0], 0)) if not p.slot_ids]
            notes = work.notes + [f"{u}: {work.placements[u].check or work.placements[u].reason}" for u in unplaced]
            sections.append(SectionSlotPlan(
                target_section_id=work.target.section_id,
                source_section_ids=list(dict.fromkeys(s for m in work.mappings for s in m.source_section_ids)),
                slot_mappings=mappings,
                unplaced_unit_ids=unplaced,
                callout_assignments=work.callouts,
                region_choices=work.regions,
                notes=notes,
            ))
        return SlotPlan(job_id=job_id, version=version, origin=origin, sections=sections,
                        gwp_guide_id=self.rules.guide_id if self.rules else None)


# ── LLM call ──────────────────────────────────────────────────────────


@dataclass
class LLMResult:
    corrections: SlotPlanCorrections
    usage: dict[str, int]
    problems: list[str]


def _usage(raw: Any) -> dict[str, int]:
    meta = getattr(raw, "usage_metadata", None) or {}
    return {k: int(meta.get(k, 0)) for k in ("input_tokens", "output_tokens", "total_tokens") if k in meta}


async def run_confirm(planner: SlotPlanner, proposal: SlotProposal, block: Block, chain: Any, prompt: str,
                      rate_limiter: Any = None) -> LLMResult:
    """One call, plus one repair retry if items are invalid. Items still invalid after that are dropped by ``apply``."""
    messages = [SystemMessage(content=SLOT_PLANNER_SYSTEM_PROMPT), HumanMessage(content=prompt)]
    usage: dict[str, int] = {"calls": 0}

    async def call(msgs):
        coro = lambda: chain.ainvoke(msgs)  # noqa: E731
        result = await (rate_limiter.execute(coro, task_name="slot_planner") if rate_limiter else coro())
        usage["calls"] += 1
        if isinstance(result, dict) and "parsed" in result:
            for k, v in _usage(result.get("raw")).items():
                usage[k] = usage.get(k, 0) + v
            if result.get("parsed") is None:
                raise ValueError(f"unparseable slot planner output: {result.get('parsing_error')}")
            parsed = result["parsed"]
        else:
            parsed = result
        return parsed if isinstance(parsed, SlotPlanCorrections) else SlotPlanCorrections.model_validate(parsed)

    corrections = await call(messages)
    problems = planner.check_corrections(proposal, block, corrections)
    if problems:
        logger.info(f"Slot planner: {len(problems)} invalid item(s), asking once for a repair")
        repair = HumanMessage(content=(
            "Your answer had problems:\n- " + "\n- ".join(problems)
            + "\n\nYour previous answer was:\n" + json.dumps(corrections.model_dump(mode="json"))
            + "\n\nReturn the full corrected answer (changes, callouts and flags)."
        ))
        corrections = await call(messages + [repair])
        problems = planner.check_corrections(proposal, block, corrections)
    return LLMResult(corrections, usage, problems)

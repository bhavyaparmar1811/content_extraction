"""Phase 8: slot planner — signals, rule proposal, LLM confirm/correct, token budgeting, validator, stage, API.

The real-sample test runs the section and slot planners with the LLM off and compares the slot plans with the
reviewed golden slot expectations (``documents/golden``).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.v2 import (
    ArtifactKind,
    CalloutKind,
    CalloutStyle,
    ConditionalKind,
    ConditionalRegion,
    ContentType,
    FormattingProfile,
    Gate,
    GwpRule,
    GwpRuleSet,
    JobMode,
    JobStatus,
    MappingOrigin,
    MappingStatus,
    MappingType,
    MigrationAction,
    RuleCategory,
    RuleStatus,
    SectionMapping,
    SectionPlan,
    SlotIcon,
    SlotPlan,
    SourceAsset,
    SourceDocument,
    SourceSection,
    SourceUnit,
    TableCell,
    TableRef,
    TargetSection,
    TargetSlot,
    TemplateModel,
    UnitType,
)
from app.services.llm.prompts.v2.slot_planner import PROMPT_VERSION as SLOT_PLANNER_PROMPT_VERSION
from app.services.migration_v2.gwp.baseline import baseline_rules
from app.services.migration_v2.planning.budget import Line, SectionLines, estimate_tokens, pack, split_section
from app.services.migration_v2.planning.slot_planner import (
    CalloutProposal,
    SlotChange,
    SlotFlag,
    SlotPlanCorrections,
    SlotPlanner,
    run_confirm,
)
from app.services.migration_v2.planning.slot_signals import icon_groups, inline_choice, table_scores, text_scores
from app.services.migration_v2.planning.slot_validator import validate_slot_plan

ROOT = Path(__file__).resolve().parent.parent


# ── Synthetic template (shaped like the main GP template) ─────────────


def _slot(sid, key, ct, instruction, order, required=False, icon=False, profile=None, callout=None):
    return TargetSlot(
        slot_id=f"{sid}-{key.upper()}", section_id=sid, key=key, instruction=instruction, content_type=ContentType(ct),
        required=required, display_order=order, icon=SlotIcon(icon_key=f"icon_{key}") if icon else None,
        formatting_profile=FormattingProfile(profile) if profile else (FormattingProfile.CALLOUT if callout else None),
        callout_kind=CalloutKind(callout) if callout else None,
    )


def template() -> TemplateModel:
    s = []
    s.append(TargetSection(section_id="TGT-1", key="PURPOSE", heading="PURPOSE", level=1, display_order=1, required=True,
        slots=[_slot("TGT-1", "what", "purpose", "Brief description of what the document is about?", 0, True, True),
               _slot("TGT-1", "intention", "purpose", "Brief description of the intention. What do you want to achieve?", 1, True, True)],
        conditional_regions=[ConditionalRegion(region_id="p:26", kind=ConditionalKind.INLINE_CHOICE,
                                               text="This Directive/SOP/Work Instruction/Guidance:")]))
    s.append(TargetSection(section_id="TGT-2", key="APPLICABILITY", heading="APPLICABILITY", level=1, display_order=2, required=True,
        slots=[_slot("TGT-2", "roles", "scope", "Explain which target roles must follow this document.", 0, True, True),
               _slot("TGT-2", "units", "scope", "In which business units, group functions, departments the document applies.", 1, True, True),
               _slot("TGT-2", "geography", "scope", "The applicable geography, e.g., world-wide, country or site(s).", 2, True, True),
               _slot("TGT-2", "processes", "scope", "If applicable, list affected processes/systems and/or materials.", 3, False, True),
               _slot("TGT-2", "not_covered", "scope", "Identify topics not covered by this document.", 4)],
        conditional_regions=[ConditionalRegion(region_id="p:32", kind=ConditionalKind.INLINE_CHOICE,
                                               text="This Directive/SOP/Work Instruction/Guidance is applicable:")]))
    s.append(TargetSection(section_id="TGT-3", key="DEFINITIONS", heading="DEFINITIONS & ABBREVIATIONS", level=1, display_order=3,
        required=True,
        slots=[_slot("TGT-3", "terms", "definition", "Add definitions for terms in alphabetical order.", 0, True, profile="table"),
               _slot("TGT-3", "abbreviations", "definition", "Fill the table: Abbreviation | Description", 1, True, profile="table")]))
    s.append(TargetSection(section_id="TGT-5", key="ROLES", heading="ROLES & RESPONSIBILITIES", level=1, display_order=5,
        required=True, one_of=[["roles", "raci"]],
        slots=[_slot("TGT-5", "roles", "responsibility", "List all roles that have responsibilities in the process.", 0, profile="table"),
               _slot("TGT-5", "raci", "responsibility", "Table B: specify the section(s) applicable to each role, use x to indicate.", 1,
                     profile="table")]))
    s.append(TargetSection(section_id="TGT-6", key="PROCESS", heading="PROCESS", level=1, display_order=6, required=True,
        slots=[_slot("TGT-6", "content", "ordered_procedure", "High level description of the process.", 0, True),
               _slot("TGT-6", "callout_attention", "warning", "Attention box.", 1, callout="attention")]))
    s.append(TargetSection(section_id="TGT-9", key="DISTRIBUTION", heading="distribution of controlled prints", level=1,
        display_order=9, slots=[_slot("TGT-9", "content", "supporting_information", "Distribution.", 0)]))
    s.append(TargetSection(section_id="TGT-10", key="DOCUMENT_HISTORY", heading="DOCUMENT HISTORY", level=1, display_order=10,
        required=True, slots=[_slot("TGT-10", "entries", "record", "Fill the table: Version | Description | Author", 0, True,
                                    profile="table")]))
    palette = [CalloutStyle(kind=CalloutKind.ATTENTION, label="Attention", fill_hex="F5CDB9"),
               CalloutStyle(kind=CalloutKind.INTRODUCTION, label="Introduction/Executive Summary", fill_hex="D2F2F7")]
    return TemplateModel(template_id="TPL", template_version=1, source_file="t.docx", sections=s, callout_palette=palette)


# ── Synthetic SOP ─────────────────────────────────────────────────────


class _Doc:
    def __init__(self):
        self.sections: list[SourceSection] = []
        self.seq = 0

    def section(self, sid, number, heading, units=(), boilerplate=False, parent=None):
        out = []
        for i, u in enumerate(units, start=1):
            kind, text, *extra = u if isinstance(u, tuple) else ("paragraph", u)
            table_ref, assets = None, []
            if kind in ("row", "definition", "reference"):
                headers, cells, table = extra[0], extra[1], extra[2] if len(extra) > 2 else "T1"
                table_ref = TableRef(table_id=f"{sid}-{table}", row_index=i, header_cells=headers,
                                     cells=[TableCell(col=c, text=t) for c, t in enumerate(cells)])
                kind = "table_row" if kind == "row" else kind
            if kind == "icon":
                kind = "paragraph"
                assets = [SourceAsset(kind="icon", asset_key="icon_abc")]
            if kind == "figure":
                assets = [SourceAsset(kind="figure", asset_key="fig_1")]
            out.append(SourceUnit(unit_id=f"{sid}-U{i:03d}", content_hash=f"{sid}{i}", section_id=sid, seq=self.seq,
                                  unit_type=UnitType(kind), text=text, table_ref=table_ref, assets=assets,
                                  is_boilerplate=boilerplate))
            self.seq += 1
        self.sections.append(SourceSection(section_id=sid, number=number, heading=heading, level=1 if not parent else 2,
                                           section_order=len(self.sections), parent_id=parent, units=out))
        return self

    def build(self) -> SourceDocument:
        return SourceDocument(document_id="SOP", source_file="sop.docx", file_type="docx", sections=self.sections)


ABBR = ["Abbreviation", "Description"]
TERM = ["Term", "Definition"]
ROLE = ["Role", "Responsibility"]
RACI = ["Activity", "Process Owner", "QA"]


def sop() -> SourceDocument:
    d = _Doc()
    d.section("SRC-1", "1", "PURPOSE", units=[
        "This SOP:",
        "This SOP describes how deviations are recorded and assessed.",
        "Both documents build the framework in order to keep deviations under control.",
    ])
    d.section("SRC-2", "2", "SCOPE", units=[
        "This procedure is binding for all employees and contractors working in the Quality division and the "
        "Logistics department, independent of the country or site.",
        "Machine learning tools are not in scope.",
    ])
    d.section("SRC-3", "3", "DEFINITIONS", units=[
        "The following terms are used:",
        ("definition", "Deviation | A departure from an approved instruction", TERM, ["Deviation", "A departure"], "T1"),
        ("figure", "[Figure]"),
        ("caption", "Image 1: The deviation landscape"),
        ("definition", "QA | Quality Assurance", ABBR, ["QA", "Quality Assurance"], "T2"),
        ("definition", "SOP | Standard Operating Procedure", ABBR, ["SOP", "Standard Operating Procedure"], "T2"),
    ])
    d.section("SRC-5", "5", "ROLES & RESPONSIBILITIES", units=[
        ("row", "Process Owner | Owns the process", ROLE, ["Process Owner", "Owns the process"], "T1"),
        ("row", "QA | Approves deviations", ROLE, ["QA", "Approves deviations"], "T1"),
        "The following table defines the responsibilities per activity:",
        ("row", "Record | R | C", RACI, ["Record", "R", "C"], "T2"),
        ("row", "Approve | C | A", RACI, ["Approve", "C", "A"], "T2"),
        ("row", "Task | Abbreviation", ["Task", "Abbreviation"], ["Create", "C"], "T3"),
    ])
    d.section("SRC-6", "6", "PROCEDURE", units=[
        "The Process Owner must record every deviation.",
        ("warning", "Never close a deviation without QA approval."),
        "The following actions must not be taken:",
        ("bullet", "Deleting a record"), ("bullet", "Backdating an entry"),
        ("procedure_step", "Assess the impact."),
    ])
    d.section("SRC-9", "9", "DOCUMENT HISTORY", units=[
        ("row", "1.0 | First version | J. Doe", ["Version", "Description of Changes", "Author"], ["1.0", "First version", "J. Doe"]),
    ], boilerplate=True)
    return d.build()


def section_plan(doc: SourceDocument) -> SectionPlan:
    m = [
        SectionMapping(target_section_id="TGT-1", source_section_ids=["SRC-1"], mapping_type=MappingType.ONE_TO_ONE),
        SectionMapping(target_section_id="TGT-2", source_section_ids=["SRC-2"], mapping_type=MappingType.ONE_TO_ONE),
        SectionMapping(target_section_id="TGT-3", source_section_ids=["SRC-3"], mapping_type=MappingType.ONE_TO_ONE),
        SectionMapping(target_section_id="TGT-5", source_section_ids=["SRC-5"], mapping_type=MappingType.ONE_TO_ONE),
        SectionMapping(target_section_id="TGT-6", source_section_ids=["SRC-6"], mapping_type=MappingType.ONE_TO_ONE),
        SectionMapping(target_section_id="TGT-9", mapping_type=MappingType.UNRESOLVED, status=MappingStatus.NOT_APPLICABLE),
        SectionMapping(target_section_id="TGT-10", source_section_ids=["SRC-9"], mapping_type=MappingType.ONE_TO_ONE),
    ]
    return SectionPlan(job_id="J", version=1, mappings=m)


def gwp(status: RuleStatus = RuleStatus.APPROVED) -> GwpRuleSet:
    def rule(rid, cat, text, types=()):
        return GwpRule(rule_id=rid, category=cat, text=text, applies_to_content_types=list(types), status=status)
    return GwpRuleSet(guide_id="GWP", version=1, rules=baseline_rules() + [
        rule("STY-001", RuleCategory.STYLE, "Use active voice."),
        rule("STY-002", RuleCategory.STYLE, "Keep scope statements short.", [ContentType.SCOPE]),
        rule("STR-001", RuleCategory.STRUCTURAL, "Clarify for whom the document applies in short sentences.", [ContentType.SCOPE]),
        rule("FMT-001", RuleCategory.FORMATTING, "Use the Attention infographic for things not to mix up."),
    ])


def _plan(rules=None, doc=None):
    doc = doc or sop()
    planner = SlotPlanner(doc, template(), section_plan(doc), rules)
    proposal = planner.propose()
    return planner, proposal, planner.build_plan(proposal, "J", 1)


def _slots(plan: SlotPlan, section: str) -> dict[str, object]:
    sec = plan.section(section)
    return {m.slot_id.split("-", 2)[2].lower(): m for m in sec.slot_mappings}


# ── Signals ───────────────────────────────────────────────────────────


def test_text_cues_come_from_the_slot_instructions():
    slots = template().sections[1].slots
    sc = text_scores("This SOP is binding for employees and contractors at every site in each country.", slots)
    by_key = {s.key: s.slot_id for s in slots}
    assert sc.score[by_key["roles"]] > 0 and sc.strong[by_key["roles"]]
    assert sc.strong[by_key["geography"]] and not sc.strong[by_key["units"]]
    # A slot with an unrelated instruction activates no concept.
    odd = TargetSlot(slot_id="X", section_id="TGT-2", key="color", instruction="Pick a colour.", content_type="scope",
                     display_order=9)
    assert text_scores("employees world-wide", [odd]).score["X"] == 0


def test_table_scores_by_header_and_raci_codes():
    doc = sop()
    tpl = template()
    units = {u.unit_id: u for u in doc.iter_units()}
    defs, roles = tpl.sections[2].slots, tpl.sections[3].slots
    abbr = table_scores([units["SRC-3-U005"], units["SRC-3-U006"]], defs)
    assert abbr["TGT-3-ABBREVIATIONS"] > abbr["TGT-3-TERMS"]
    raci = table_scores([units["SRC-5-U004"], units["SRC-5-U005"]], roles)
    assert raci["TGT-5-RACI"] > raci["TGT-5-ROLES"]
    role = table_scores([units["SRC-5-U001"]], roles)
    assert role["TGT-5-ROLES"] > role["TGT-5-RACI"]


def test_inline_choice_and_icon_groups():
    regions = template().sections[1].conditional_regions
    unit = SourceUnit(unit_id="U", content_hash="h", section_id="S", seq=0, unit_type="paragraph", text="This SOP is applicable:")
    region, choice = inline_choice(unit, regions)
    assert (region.region_id, choice) == ("p:32", "SOP")
    other = unit.model_copy(update={"text": "The following steps apply:"})
    assert inline_choice(other, regions) is None

    d = _Doc().section("S", "1", "X", units=[("icon", "Valid for QA staff."), "Quality Assurance", "Global IMP Delivery",
                                             ("icon", "World-wide"), "This is a full sentence that stands alone."])
    groups = icon_groups(d.build().sections[0].units)
    assert [[u.unit_id for u in g] for g in groups] == [["S-U001", "S-U002", "S-U003"], ["S-U004"], ["S-U005"]]


# ── Rule proposal ─────────────────────────────────────────────────────


def test_purpose_and_applicability_by_cues_regions_and_shared_sentences():
    _, _, plan = _plan()
    purpose = _slots(plan, "TGT-1")
    assert purpose["what"].source_unit_ids == ["SRC-1-U002"]
    assert purpose["intention"].source_unit_ids == ["SRC-1-U003"]
    assert plan.section("TGT-1").region_choices[0].model_dump() == {"region_id": "p:26", "unit_ids": ["SRC-1-U001"], "choice": "SOP"}

    app = _slots(plan, "TGT-2")
    # One sentence names the roles, the business unit and the geography: it feeds all three, each with its part.
    for key in ("roles", "units", "geography"):
        assert app[key].source_unit_ids == ["SRC-2-U001"] and app[key].extraction_scope == [key]
    assert app["not_covered"].source_unit_ids == ["SRC-2-U002"]
    assert app["processes"].status == MappingStatus.NOT_APPLICABLE  # optional, nothing in the source


def test_icon_rows_map_by_position_when_counts_match():
    d = _Doc()
    d.section("SRC-1", "1", "PURPOSE", units=[("icon", "Shipping of goods."), ("icon", "Keep goods compliant.")])
    d.section("SRC-2", "2", "APPLICABILITY", units=[
        "This SOP is applicable:",
        ("icon", "Valid for the shipping team."), ("icon", "Quality Assurance"), "Global IMP Delivery",
        ("icon", "World-wide"), ("icon", "Transport Qualifications"), "Materials and Goods",
    ])
    doc = d.build()
    planner = SlotPlanner(doc, template(), SectionPlan(job_id="J", version=1, mappings=[
        SectionMapping(target_section_id="TGT-1", source_section_ids=["SRC-1"], mapping_type=MappingType.ONE_TO_ONE),
        SectionMapping(target_section_id="TGT-2", source_section_ids=["SRC-2"], mapping_type=MappingType.ONE_TO_ONE)]))
    plan = planner.build_plan(planner.propose(), "J", 1)
    purpose, app = _slots(plan, "TGT-1"), _slots(plan, "TGT-2")
    assert purpose["what"].source_unit_ids == ["SRC-1-U001"] and purpose["intention"].source_unit_ids == ["SRC-1-U002"]
    assert app["roles"].source_unit_ids == ["SRC-2-U002"]
    assert app["units"].source_unit_ids == ["SRC-2-U003", "SRC-2-U004"]  # the label line joins its icon row
    assert app["geography"].source_unit_ids == ["SRC-2-U005"]
    assert app["processes"].source_unit_ids == ["SRC-2-U006", "SRC-2-U007"]
    assert plan.section("TGT-2").region_choices[0].unit_ids == ["SRC-2-U001"]


def test_tables_go_whole_and_text_follows_its_table():
    _, _, plan = _plan()
    defs = _slots(plan, "TGT-3")
    assert defs["terms"].source_unit_ids == ["SRC-3-U001", "SRC-3-U002"]  # the intro line goes with the table it introduces
    # Figure and caption between the tables go below the section's tables: the last table slot, after its rows.
    assert defs["abbreviations"].source_unit_ids == ["SRC-3-U003", "SRC-3-U004", "SRC-3-U005", "SRC-3-U006"]
    assert defs["abbreviations"].below_unit_ids == ["SRC-3-U003", "SRC-3-U004"]
    assert defs["abbreviations"].status == MappingStatus.MAPPED and defs["terms"].below_unit_ids == []
    assert plan.section("TGT-3").unplaced_unit_ids == []

    roles = _slots(plan, "TGT-5")
    assert roles["roles"].source_unit_ids == ["SRC-5-U001", "SRC-5-U002"]
    assert roles["raci"].source_unit_ids == ["SRC-5-U003", "SRC-5-U004", "SRC-5-U005", "SRC-5-U006"]  # intro and legend too


def test_one_of_gap_and_section_level_gaps():
    doc = sop()
    doc.sections[3].units = [u for u in doc.sections[3].units if u.unit_id in ("SRC-5-U001", "SRC-5-U002")]
    _, _, plan = _plan(doc=doc)
    roles = _slots(plan, "TGT-5")
    assert roles["raci"].status == MappingStatus.NOT_APPLICABLE and "one of roles / raci" in roles["raci"].note

    doc.sections[3].units = []
    _, _, plan = _plan(doc=doc)
    roles = _slots(plan, "TGT-5")
    assert roles["roles"].status == MappingStatus.SOURCE_CONTENT_NOT_FOUND
    assert roles["raci"].status == MappingStatus.NOT_APPLICABLE
    assert _slots(plan, "TGT-9")["content"].status == MappingStatus.NOT_APPLICABLE  # optional section removed
    history = _slots(plan, "TGT-10")["entries"]
    assert history.source_unit_ids == ["SRC-9-U001"]  # all-boilerplate history rows are still the section's content


def test_single_slot_section_and_rule_callouts():
    _, proposal, plan = _plan()
    process = _slots(plan, "TGT-6")
    assert process["content"].source_unit_ids == [f"SRC-6-U00{i}" for i in range(1, 7)]
    callouts = plan.section("TGT-6").callout_assignments
    assert [(c.unit_ids, c.kind.value, c.origin.value) for c in callouts] == [(["SRC-6-U002"], "attention", "rule")]
    assert process["callout_attention"].source_unit_ids == ["SRC-6-U002"]  # the template's own box takes it


def test_placement_mode_copies_and_gwp_rules_drive_actions():
    _, _, plain = _plan()
    mapped = [m for s in plain.sections for m in s.slot_mappings if m.source_unit_ids]
    assert {m.migration_action for m in mapped} == {MigrationAction.COPY_VERBATIM}
    assert plain.gwp_guide_id is None

    _, _, styled = _plan(gwp())
    app, defs = _slots(styled, "TGT-2"), _slots(styled, "TGT-3")
    assert app["roles"].migration_action == MigrationAction.EXTRACT_AND_REWRITE
    assert defs["terms"].migration_action == MigrationAction.COPY_VERBATIM  # tables are data
    assert {"STY-001", "STY-002", "FMT-001", "PRES-001"} <= set(app["roles"].rule_ids)
    assert "STY-002" not in _slots(styled, "TGT-6")["content"].rule_ids  # scope-only rule
    assert styled.gwp_guide_id == "GWP"

    _, _, unapproved = _plan(gwp(RuleStatus.CANDIDATE))  # candidates never reach a migration
    roles = _slots(unapproved, "TGT-2")["roles"]
    assert roles.migration_action == MigrationAction.COPY_VERBATIM
    assert set(roles.rule_ids) == {r.rule_id for r in baseline_rules()}


def test_rule_plan_validates_clean():
    doc = sop()
    _, _, plan = _plan(doc=doc)
    report = validate_slot_plan(plan, section_plan(doc), doc, template())
    assert report.open_gate_issues == [] and report.unit_coverage == 1.0
    assert not any("fit no slot" in i.message for i in report.issues)  # every passage has a slot


# ── Prompt and LLM ────────────────────────────────────────────────────


def test_prompt_carries_slots_palette_and_the_jobs_gwp_rules():
    planner, proposal, _ = _plan(gwp())
    work = proposal.work("TGT-2")
    work.placements["SRC-2-U002"].check = "unsure"  # a doubt brings APPLICABILITY into the prompt
    blocks = planner.blocks(proposal)
    assert len(blocks) == 1
    prompt = planner.prompt(proposal, blocks[0])
    assert "APPLICABILITY.geography | scope | required" in prompt
    assert "## APPLICABILITY | APPLICABILITY (source: SRC-2; no callout boxes)" in prompt
    assert "## PROCESS | PROCESS (source: SRC-6; callout boxes allowed)" in prompt
    assert "## PURPOSE" not in prompt  # no doubts and no callout boxes: not sent
    assert "attention = Attention" in prompt and "introduction = Introduction/Executive Summary" in prompt
    # The GWP rules in the prompt are the job's own rule set, not built into the prompt.
    assert "STR-001: Clarify for whom the document applies" in prompt and "FMT-001: Use the Attention" in prompt
    assert "STY-001" not in prompt  # style rules are for the drafter
    assert "[CALLOUT: attention]" in prompt
    assert "SRC-3-U002" not in prompt or "[CHECK" in prompt  # table-only sections without doubts are left out

    planner_plain, proposal_plain, _ = _plan()
    plain = planner_plain.prompt(proposal_plain, planner_plain.blocks(proposal_plain)[0])
    assert "GWP RULES" not in plain


def test_llm_changes_callouts_and_flags_are_applied():
    planner, proposal, _ = _plan()
    for unit_id in ("SRC-1-U002", "SRC-2-U002"):
        proposal.work_of_unit(unit_id).placements[unit_id].check = "unsure"
    block = planner.blocks(proposal)[0]
    corrections = SlotPlanCorrections(
        changes=[SlotChange(unit_ids=["SRC-2-U002"], slot_refs=["APPLICABILITY.not_covered", "APPLICABILITY.processes"],
                            reason="names an excluded tool and a system")],
        callouts=[CalloutProposal(unit_ids=["SRC-6-U003"], kind="attention", reason="prohibitions")],
        flags=[SlotFlag(unit_ids=["SRC-1-U002"], note="may also state the intention")],
    )
    assert planner.check_corrections(proposal, block, corrections) == []
    assert planner.apply(proposal, block, corrections) == []
    plan = planner.build_plan(proposal, "J", 2)
    app = _slots(plan, "TGT-2")
    assert app["processes"].source_unit_ids == ["SRC-2-U002"] and app["processes"].origin == MappingOrigin.LLM
    callout = plan.section("TGT-6").callout_assignments[-1]
    assert callout.unit_ids == ["SRC-6-U003", "SRC-6-U004", "SRC-6-U005"]  # the intro takes its list
    assert callout.origin.value == "llm"
    what = _slots(plan, "TGT-1")["what"]
    assert what.status == MappingStatus.NEEDS_REVIEW and "LLM: may also state" in what.note


def test_invalid_llm_items_are_reported_and_dropped():
    planner, proposal, _ = _plan()
    for unit_id in ("SRC-1-U002", "SRC-2-U001"):
        proposal.work_of_unit(unit_id).placements[unit_id].check = "unsure"
    block = planner.blocks(proposal)[0]
    bad = SlotPlanCorrections(
        changes=[SlotChange(unit_ids=["SRC-2-U001"], slot_refs=["PURPOSE.what"], reason="other section"),
                 SlotChange(unit_ids=["SRC-1-U002", "SRC-2-U001"], slot_refs=[], reason="two sections"),
                 SlotChange(unit_ids=["SRC-99-U001"], slot_refs=["APPLICABILITY.roles"], reason="made up")],
        callouts=[CalloutProposal(unit_ids=["SRC-6-U002"], kind="attention", reason="already a box"),
                  CalloutProposal(unit_ids=["SRC-6-U001"], kind="key_takeaway", reason="not in the palette"),
                  CalloutProposal(unit_ids=["SRC-6-U001", "SRC-6-U006"], kind="introduction", reason="gap"),
                  CalloutProposal(unit_ids=["SRC-1-U002"], kind="introduction", reason="no boxes in PURPOSE")],
    )
    problems = planner.check_corrections(proposal, block, bad)
    joined = " | ".join(problems)
    assert "not content slots of APPLICABILITY" in joined and "span several sections" in joined
    assert "not among the passages shown" in joined and "already in a callout" in joined
    assert "not in the template palette" in joined and "consecutive passages" in joined
    assert "PURPOSE has no callout boxes" in joined
    dropped = planner.apply(proposal, block, bad)
    assert len(dropped) == 7
    plan = planner.build_plan(proposal, "J", 2)
    assert _slots(plan, "TGT-2")["roles"].source_unit_ids == ["SRC-2-U001"]  # unchanged


def test_the_llm_never_drops_a_placed_passage_or_moves_one_below_the_tables():
    planner, proposal, _ = _plan()
    proposal.work_of_unit("SRC-3-U001").placements["SRC-3-U001"].check = "unsure"  # brings DEFINITIONS into the prompt
    block = planner.blocks(proposal)[0]
    prompt = planner.prompt(proposal, block)
    assert "SRC-3-U003 | figure | → abbreviations [BELOW TABLES]" in prompt
    bad = SlotPlanCorrections(changes=[
        SlotChange(unit_ids=["SRC-3-U001"], slot_refs=[], reason="an intro line, not a term"),
        SlotChange(unit_ids=["SRC-3-U003", "SRC-3-U004"], slot_refs=["DEFINITIONS.terms"], reason="figure of the terms"),
        SlotChange(unit_ids=["SRC-3-U004"], slot_refs=[], reason="figures and captions are supplementary"),
    ])
    joined = " | ".join(planner.check_corrections(proposal, block, bad))
    assert "a passage is never dropped" in joined and joined.count("below the section's tables by rule") == 2
    assert len(planner.apply(proposal, block, bad)) == 3
    plan = planner.build_plan(proposal, "J", 2)
    defs = _slots(plan, "TGT-3")
    assert defs["terms"].source_unit_ids[0] == "SRC-3-U001" and plan.section("TGT-3").unplaced_unit_ids == []
    assert defs["abbreviations"].below_unit_ids == ["SRC-3-U003", "SRC-3-U004"]


def test_a_long_narrative_with_its_figure_goes_below_the_tables_but_an_intro_line_stays():
    long = "The GBS Solution operates across functions and companies to leverage productivity and compliance. " * 4
    doc = sop()
    defs = next(s for s in doc.sections if s.section_id == "SRC-3")
    units = {u.unit_id: u for u in defs.units}
    units["SRC-3-U003"].unit_type, units["SRC-3-U003"].text = UnitType.PARAGRAPH, long.strip()  # narrative, no figure
    _, _, plan = _plan(doc=doc)
    abbr = _slots(plan, "TGT-3")["abbreviations"]
    assert abbr.below_unit_ids == ["SRC-3-U003", "SRC-3-U004"]  # the narrative and the caption after it
    assert _slots(plan, "TGT-3")["terms"].source_unit_ids == ["SRC-3-U001", "SRC-3-U002"]  # the intro stays


class FakeChain:
    def __init__(self, *outputs, usage=None):
        self.outputs = list(outputs)
        self.calls = []
        self.usage = usage or {"input_tokens": 900, "output_tokens": 30, "total_tokens": 930}

    async def ainvoke(self, messages):
        self.calls.append(messages)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"raw": SimpleNamespace(usage_metadata=self.usage), "parsed": out, "parsing_error": None}


async def test_run_confirm_repairs_once():
    planner, proposal, _ = _plan()
    for unit_id in ("SRC-1-U002", "SRC-2-U001"):
        proposal.work_of_unit(unit_id).placements[unit_id].check = "unsure"
    block = planner.blocks(proposal)[0]
    bad = SlotPlanCorrections(changes=[SlotChange(unit_ids=["SRC-2-U001"], slot_refs=["APPLICABILITY.nowhere"], reason="x")])
    good = SlotPlanCorrections()
    chain = FakeChain(bad, good)
    result = await run_confirm(planner, proposal, block, chain, planner.prompt(proposal, block))
    assert result.problems == [] and result.usage["calls"] == 2 and result.usage["input_tokens"] == 1800
    assert "not content slots" in chain.calls[1][-1].content


# ── Budget ────────────────────────────────────────────────────────────


def test_budget_splits_at_coherent_boundaries_and_packs():
    lines = [Line("intro line that ends with a colon:" + "x" * 80, ["U1"]),
             Line("item one " + "x" * 80, ["U2"], break_ok=False), Line("item two " + "x" * 80, ["U3"], break_ok=False),
             Line("next paragraph " + "x" * 80, ["U4"]), Line("last paragraph " + "x" * 80, ["U5"])]
    big = SectionLines("TGT-6", "## PROCESS", lines)
    chunks = split_section(big, budget=60)
    assert [[l.unit_ids[0] for l in c.lines] for c in chunks] == [["U1", "U2", "U3"], ["U4", "U5"]]  # list stays with intro
    assert chunks[0].header.endswith("(part 1 of 2)")
    small = SectionLines("TGT-1", "## PURPOSE", [Line("short", ["P1"])])
    blocks = pack([small, big, small], budget=60)
    assert [b.target_section_ids for b in blocks][0] == ["TGT-1"]
    assert set().union(*(b.unit_ids for b in blocks)) == {"P1", "U1", "U2", "U3", "U4", "U5"}
    assert all(b.tokens <= 60 or len(b.parts) == 1 for b in blocks)  # only a lone oversized chunk exceeds the budget
    assert estimate_tokens("abcd" * 10) == 11


def test_large_sections_make_several_blocks():
    d = _Doc()
    d.section("SRC-6", "6", "PROCEDURE", units=[f"Step {i}: the operator must check item {i} carefully." + " x" * 60 for i in range(60)])
    doc = d.build()
    planner = SlotPlanner(doc, template(), SectionPlan(job_id="J", version=1, mappings=[
        SectionMapping(target_section_id="TGT-6", source_section_ids=["SRC-6"], mapping_type=MappingType.ONE_TO_ONE)]))
    proposal = planner.propose()
    blocks = planner.blocks(proposal, budget=800)
    assert len(blocks) > 1
    assert set().union(*(b.unit_ids for b in blocks)) == {u.unit_id for u in doc.iter_units()}


# ── Validator ─────────────────────────────────────────────────────────


def test_validator_gates():
    doc = sop()
    sp = section_plan(doc)
    _, _, plan = _plan(doc=doc)
    tpl = template()

    broken = plan.model_copy(deep=True)
    purpose = broken.section("TGT-1")
    purpose.slot_mappings[0].source_unit_ids = []  # 'what' loses its passage...
    purpose.slot_mappings[0].status = MappingStatus.NOT_APPLICABLE  # ...and is not flagged as a gap
    app = broken.section("TGT-2")
    app.slot_mappings[3].source_unit_ids = ["SRC-6-U001"]  # a passage of another section
    app.slot_mappings[3].status = MappingStatus.MAPPED
    defs = broken.section("TGT-3")
    defs.slot_mappings[1].source_unit_ids = ["SRC-3-U006", "SRC-3-U005"]  # out of order
    broken.sections = [s for s in broken.sections if s.target_section_id != "TGT-10"]

    report = validate_slot_plan(broken, sp, doc, tpl)
    gates = {(i.gate, i.target_section_id) for i in report.open_gate_issues}
    assert (Gate.UNACCOUNTED_SOURCE, "TGT-1") in gates  # SRC-1-U002 is nowhere
    assert (Gate.MISSING_SLOT, "TGT-1") in gates
    assert (Gate.UNACCOUNTED_SOURCE, "TGT-2") in gates  # outside its scope
    assert (Gate.SEQUENCE_VIOLATION, "TGT-3") in gates
    assert (Gate.MISSING_SLOT, "TGT-10") in gates
    assert report.unit_coverage < 1.0


# ── Stage, orchestrator and API ───────────────────────────────────────

from tests.test_v2_migration_jobs import END_STATUS, Env, _events, _wait_for  # noqa: E402


class FakeFactory:
    """One chain per output schema, so the section and slot planners get their own fakes."""

    def __init__(self, chains: dict):
        self.chains = chains

    def create_structured_planner(self, schema, include_raw=False):
        return self.chains[schema.__name__]

    def planner_label(self):
        return "azure/fake"


async def test_stage_runs_the_llm_per_block_and_records_usage(tmp_path):
    from app.services.migration_v2.planning.section_planner import SectionPlanCorrections

    env = Env(tmp_path)
    orch = env.orchestrator()
    orch.chain_factory = FakeFactory({"SectionPlanCorrections": FakeChain(SectionPlanCorrections()),
                                      "SlotPlanCorrections": FakeChain(SlotPlanCorrections())})
    job = await orch.wait(env.create(orch).job_id)
    plan = orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan)
    assert (plan.origin.value, plan.model, plan.prompt_version) == ("llm", "azure/fake", SLOT_PLANNER_PROMPT_VERSION)
    assert plan.token_usage["calls"] == 1 and plan.token_usage["input_tokens"] == 900
    event = _events(env.store, job.job_id, "slot_planner_llm")[0]["detail"]
    assert event["blocks"] == 1 and event["gwp_rules_in_prompt"] == []  # no GWP named: placement mode
    assert job.latest_artifact(ArtifactKind.QUALITY_REPORT, "slot_plan") is not None

    orch.chain_factory = FakeFactory({"SectionPlanCorrections": FakeChain(SectionPlanCorrections()),
                                      "SlotPlanCorrections": FakeChain(RuntimeError("Azure timeout"))})
    job = await orch.wait(env.create(orch).job_id)
    plan = orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan)
    assert plan.origin.value == "rule" and plan.token_usage == {}
    assert "Azure timeout" in _events(env.store, job.job_id, "slot_planner_llm_failed")[0]["detail"]["error"]


async def test_stage_sends_the_named_guides_rules(tmp_path):
    from app.services.migration_v2.planning.section_planner import SectionPlanCorrections

    env = Env(tmp_path)
    guide = env.approved_guide("GWP")
    rules = GwpRuleSet.model_validate_json(Path(guide["rules_path"]).read_text(encoding="utf-8"))
    extra = GwpRule(rule_id="STR-042", category=RuleCategory.STRUCTURAL, text="Put deviation timelines in the steps.",
                    status=RuleStatus.APPROVED)
    Path(guide["rules_path"]).write_text(rules.model_copy(update={"rules": rules.rules + [extra]}).model_dump_json(),
                                         encoding="utf-8")
    slot_chain = FakeChain(SlotPlanCorrections())
    orch = env.orchestrator()
    orch.chain_factory = FakeFactory({"SectionPlanCorrections": FakeChain(SectionPlanCorrections()),
                                      "SlotPlanCorrections": slot_chain})
    job = await orch.wait(env.create(orch, gwp_id="GWP").job_id)
    prompt = slot_chain.calls[0][1].content
    assert "STR-042: Put deviation timelines in the steps." in prompt
    assert "STR-042" in _events(env.store, job.job_id, "slot_planner_llm")[0]["detail"]["gwp_rules_in_prompt"]
    plan = orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan)
    assert plan.gwp_guide_id == "GWP"


@pytest.fixture
def api(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.auth import require_user
    from app.config.settings import get_settings
    from app.main import app

    env = Env(tmp_path)
    app.dependency_overrides[get_settings] = lambda: env.settings
    app.dependency_overrides[require_user] = lambda: {"userId": "reviewer-1"}
    with TestClient(app) as client:
        previous = app.state.migration_orchestrator
        orch = env.orchestrator()
        app.state.migration_orchestrator = orch
        try:
            yield client, orch, env
        finally:
            client.portal.call(orch.stop)
            app.state.migration_orchestrator = previous
            app.dependency_overrides.pop(get_settings, None)
            app.dependency_overrides.pop(require_user, None)


def test_api_slot_plan_review_validates_and_blocks_approval(api):
    client, orch, env = api
    job_id = client.post("/api/v1/migrations", json={"sop_record_id": env.sop["id"], "template_id": "TPL",
                                                     "mode": "review"}).json()["job_id"]
    _wait_for(client, job_id, {"SECTION_PLAN_REVIEW_PENDING"})
    assert client.post(f"/api/v1/migrations/{job_id}/section-plan/approve").status_code == 200
    _wait_for(client, job_id, {"SLOT_PLAN_REVIEW_PENDING"})
    plan = client.get(f"/api/v1/migrations/{job_id}/slot-plan").json()
    assert client.get(f"/api/v1/migrations/{job_id}/slot-plan/validation").json()["issues"] is not None

    # A reviewer empties every slot: the passages are unaccounted for.
    broken = json.loads(json.dumps(plan))
    for section in broken["sections"]:
        for m in section["slot_mappings"]:
            m.update({"source_unit_ids": [], "status": "not_applicable", "migration_action": "none"})
            m.pop("note", None)
    saved = client.patch(f"/api/v1/migrations/{job_id}/slot-plan", json=broken)
    assert saved.status_code == 200, saved.text
    assert "unaccounted_source" in {i.get("gate") for i in saved.json()["validation"]["issues"]}
    assert all(m["origin"] == "human" for s in saved.json()["sections"] for m in s["slot_mappings"]
               if any(m["slot_id"] == o["slot_id"] and o.get("source_unit_ids") for o in plan["sections"][0]["slot_mappings"]))
    refused = client.post(f"/api/v1/migrations/{job_id}/slot-plan/approve")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "PLAN_HAS_GATE_ISSUES"

    fixed = client.patch(f"/api/v1/migrations/{job_id}/slot-plan", json=plan)
    assert [i for i in fixed.json()["validation"]["issues"] if i.get("gate")] == []
    assert client.post(f"/api/v1/migrations/{job_id}/slot-plan/approve").status_code == 200
    _wait_for(client, job_id, {END_STATUS.value})


# ── Real samples vs the golden slot expectations (LLM off) ────────────

GOLDEN_DIR = ROOT / "documents" / "golden"
SOP_DIR = ROOT / "documents" / "SOPs"
TEMPLATE = ROOT / "documents" / "Templates" / "Template Main GP Docs.docx"
TEMPLATE_CONFIG = ROOT / "documents" / "template_config" / "Template_Main_GP_Docs.json"


@pytest.fixture(scope="module")
def samples():
    if not (GOLDEN_DIR.exists() and TEMPLATE.exists()):
        pytest.skip("sample SOPs, template and goldens not available")
    from app.config.settings import Settings
    from app.services.migration_v2.gwp.ingest import parse_guide
    from app.services.migration_v2.template.overrides import load_config
    from app.services.migration_v2.template.service import build_template_model

    root = Path(tempfile.mkdtemp())
    settings = Settings(project_root=root)
    settings.resolve_paths(root)
    settings.ensure_directories()
    tpl = build_template_model(TEMPLATE, "TPL", 1, load_config(TEMPLATE_CONFIG)).model
    out = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        golden = json.loads(path.read_text(encoding="utf-8"))
        sop_path = SOP_DIR / golden["sop_file"]
        if sop_path.exists():
            out.append((parse_guide(sop_path, settings, "S"), golden))
    return tpl, out


def test_samples_rule_slot_plans_match_the_goldens(samples):
    from app.services.migration_v2.inspection.golden import slot_problems
    from app.services.migration_v2.planning.section_planner import SectionPlanner

    tpl, docs = samples
    assert docs
    for doc, golden in docs:
        planner = SectionPlanner(doc, tpl)
        section = planner.build_plan(planner.propose(), "J", 1)
        slots = SlotPlanner(doc, tpl, section)
        plan = slots.build_plan(slots.propose(), "J", 1)
        assert slot_problems(plan, doc, tpl, golden) == [], golden["sop_file"]
        report = validate_slot_plan(plan, section, doc, tpl)
        assert report.open_gate_issues == [] and report.unit_coverage == 1.0, golden["sop_file"]


def test_golden_slot_comparison_catches_a_wrong_plan(samples):
    from app.services.migration_v2.inspection.golden import slot_problems
    from app.services.migration_v2.planning.section_planner import SectionPlanner

    tpl, docs = samples
    doc, golden = next((d, g) for d, g in docs if "10505" in g["sop_file"])
    planner = SectionPlanner(doc, tpl)
    section = planner.build_plan(planner.propose(), "J", 1)
    slots = SlotPlanner(doc, tpl, section)
    plan = slots.build_plan(slots.propose(), "J", 1)
    purpose = next(s for s in plan.sections if s.target_section_id == tpl.slot_by_ref("PURPOSE.what").section_id)
    what, intention = purpose.slot_mappings[0], purpose.slot_mappings[1]
    what.source_unit_ids, intention.source_unit_ids = intention.source_unit_ids, what.source_unit_ids
    problems = slot_problems(plan, doc, tpl, golden)
    assert any("expected PURPOSE.what" in p for p in problems) and any("expected PURPOSE.intention" in p for p in problems)


def test_a_single_slot_section_keeps_every_passage():
    planner, proposal, _ = _plan()
    block = planner.blocks(proposal)[0]
    unplace = SlotPlanCorrections(changes=[SlotChange(unit_ids=["SRC-6-U001"], slot_refs=[], reason="fits no slot")])
    assert "one content slot" in planner.check_corrections(proposal, block, unplace)[0]
    planner.apply(proposal, block, unplace)
    assert "SRC-6-U001" in _slots(planner.build_plan(proposal, "J", 2), "TGT-6")["content"].source_unit_ids

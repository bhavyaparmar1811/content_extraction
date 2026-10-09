"""Phase 9: drafter — reference tokens, memory, placement mode, splits of shared passages, GWP rewrite with per-passage checks and
targeted retry, draft checks, stage, API, and the samples in placement mode against the goldens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.v2 import (
    ArtifactKind,
    Claim,
    ClaimKind,
    CrossReference,
    Gate,
    GwpRule,
    GwpRuleSet,
    JobStatus,
    MappingStatus,
    MigrationAction,
    QualityReport,
    RefKind,
    RefResolution,
    RuleCategory,
    RuleStatus,
    SectionDraft,
    SectionMapping,
    SectionPlan,
    SlotDraft,
    SourceDocument,
    MappingType,
)
from app.services.llm.prompts.v2.drafter import PROMPT_VERSION as DRAFTER_PROMPT_VERSION
from app.services.migration_v2.drafting.checks import draft_issues
from app.services.migration_v2.drafting.drafter import (
    GAP_TEXT,
    DraftedClaim,
    DraftedSlot,
    Drafter,
    Split,
    SplitOutput,
    SplitPiece,
    RewriteOutput,
    draft_all,
    split_passage,
)
from app.services.migration_v2.drafting.memory import DraftMemory
from app.services.migration_v2.drafting.refs import detokenized, internal_refs, tokenize
from app.services.migration_v2.planning.slot_planner import SlotPlanner
from app.services.migration_v2.quality.facts import extract_facts
from app.services.migration_v2.quality.preservation import check_preservation
from tests.test_v2_slot_planner import _Doc, gwp, section_plan, template

ROOT = Path(__file__).resolve().parent.parent


# ── Synthetic SOP with references, a sub-section and a source warning ─


def sop() -> SourceDocument:
    d = _Doc()
    d.section("SRC-1", "1", "PURPOSE", units=["This SOP describes how deviations are recorded and assessed.",
                                              "Both documents build the framework in order to keep deviations under control."])
    d.section("SRC-2", "2", "SCOPE", units=[
        "This procedure is binding for all employees and contractors working in the Quality division and the "
        "Logistics department, independent of the country or site."])
    d.section("SRC-3", "3", "DEFINITIONS", units=[
        ("definition", "QA | Quality Assurance", ["Abbreviation", "Description"], ["QA", "Quality Assurance"], "T2")])
    d.section("SRC-5", "5", "ROLES", units=[
        ("row", "Process Owner | Owns the process", ["Role", "Responsibility"], ["Process Owner", "Owns the process"], "T1")])
    d.section("SRC-6", "6", "PROCEDURE", units=[
        "The Process Owner must record every deviation within 5 days (see section 5).",
        ("warning", "Never close a deviation without QA approval."),
    ])
    d.section("SRC-6.1", "6.1", "Assessment", parent="SRC-6", units=[
        "The following actions must not be taken:",
        ("bullet", "Deleting a record"),
        ("procedure_step", "QA may extend the deadline as described in 6.2."),
        ("figure", "[Figure]"),
        ("caption", "Image 1: The deviation flow"),
    ])
    d.section("SRC-6.2", "6.2", "Closure", parent="SRC-6", units=["Refer to “Deviation Closure Guideline”."])
    d.section("SRC-9", "9", "DOCUMENT HISTORY", units=[
        ("row", "1.0 | First version | J. Doe", ["Version", "Description of Changes", "Author"], ["1.0", "First version", "J. Doe"]),
    ], boilerplate=True)
    doc = d.build()
    doc.cross_references = [
        CrossReference(ref_id="XREF-001", from_unit_id="SRC-6-U001", raw_text="section 5", ref_kind=RefKind.SECTION,
                       target="SRC-5", resolution=RefResolution.RESOLVED),
        CrossReference(ref_id="XREF-002", from_unit_id="SRC-6.1-U003", raw_text="described in 6.2", ref_kind=RefKind.SECTION,
                       target="SRC-6.2", resolution=RefResolution.RESOLVED),
        CrossReference(ref_id="XREF-003", from_unit_id="SRC-6-U001", raw_text="BI-VQD-1", ref_kind=RefKind.EXTERNAL_DOC,
                       target="BI-VQD-1", resolution=RefResolution.EXTERNAL),
    ]
    return doc


def plans(doc: SourceDocument, rules=None):
    sp = SectionPlan(job_id="J", version=1, mappings=[
        m.model_copy(update={"source_section_ids": m.source_section_ids + (["SRC-6.1", "SRC-6.2"] if m.target_section_id == "TGT-6" else [])})
        for m in section_plan(doc).mappings])
    planner = SlotPlanner(doc, template(), sp, rules)
    return sp, planner.build_plan(planner.propose(), "J", 1)


def drafter(rules=None, doc=None):
    doc = doc or sop()
    sp, slp = plans(doc, rules)
    return Drafter(doc, template(), sp, slp, extract_facts(doc, "J"), rules), doc, slp


def _claims(drafts, slot_id):
    return next(s.claims for d in drafts for s in d.slots if s.slot_id == slot_id)


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeChain:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        out = self.respond(messages[-1].content, len(self.calls))
        if isinstance(out, Exception):
            raise out
        return {"raw": SimpleNamespace(usage_metadata={"input_tokens": 500, "output_tokens": 200}), "parsed": out,
                "parsing_error": None}


class FakeFactory:
    def __init__(self, **chains):
        self.chains = chains

    def create_structured_planner(self, schema, include_raw=False):
        return self.chains.get(schema.__name__) or FakeChain(lambda prompt, n: schema())

    def planner_label(self):
        return "azure/fake"


_PASSAGE = re.compile(r"^\[(SRC-[^\]]+)\] \((\w+)\)(?: \(part: \w+\))? (.*)$")


def echo(transform=lambda unit_id, text: text, rules=("STY-001",)):
    """A rewrite fake: one claim per passage, its text passed through ``transform``."""
    def respond(prompt: str, n: int) -> RewriteOutput:
        slots, current = [], None
        for line in prompt.splitlines():
            if line.startswith("### "):
                current = DraftedSlot(slot_ref=line[4:].split(" | ")[0], claims=[])
                slots.append(current)
            elif (m := _PASSAGE.match(line)) and current is not None:
                kind = m.group(2) if m.group(2) in ("paragraph", "bullet", "step") else "paragraph"
                current.claims.append(DraftedClaim(text=transform(m.group(1), m.group(3)), source_unit_ids=[m.group(1)],
                                                   kind=kind, rule_ids_applied=list(rules)))
        return RewriteOutput(slots=slots)
    return respond


# ── Reference tokens and memory ───────────────────────────────────────


def test_internal_references_become_tokens_and_read_back():
    doc = sop()
    units = {u.unit_id: u for u in doc.iter_units()}
    refs = internal_refs(doc)
    assert "SRC-6-U001" in refs and all(r.ref_kind != RefKind.EXTERNAL_DOC for r in refs["SRC-6-U001"])
    assert tokenize(units["SRC-6-U001"], refs["SRC-6-U001"]) == \
        "The Process Owner must record every deviation within 5 days (see {{ref:SRC-5}})."
    step = tokenize(units["SRC-6.1-U003"], refs["SRC-6.1-U003"])
    assert step == "QA may extend the deadline as {{ref:SRC-6.2}}."
    draft = SectionDraft(target_section_id="TGT-6", version=1, slots=[SlotDraft(slot_id="TGT-6-CONTENT", claims=[
        Claim(claim_id="C1", text=step, source_unit_ids=["SRC-6.1-U003"])])])
    # The number "6.2" lived only in the reference: with the token read back, nothing is reported lost.
    assert check_preservation(extract_facts(doc, "J"), doc, detokenized([draft], doc)) == []
    assert any("6.2" in i.message for i in check_preservation(extract_facts(doc, "J"), doc, [draft]))


def test_memory_is_cut_to_its_cap():
    memory = DraftMemory(terms={f"A{i}": f"Term number {i}" for i in range(200)}, roles=["Process Owner", "QA"],
                         ref_targets={"SRC-5": "ROLES (ROLES & RESPONSIBILITIES)"})
    full = memory.render(10_000)
    assert "ROLE NAMES (keep exactly): Process Owner, QA" in full and "A199 = Term number 199" in full
    small = memory.render(60)
    assert "{{ref:SRC-5}} points to ROLES" in small and "Process Owner" in small and "A199" not in small


# ── Placement mode ────────────────────────────────────────────────────


async def test_placement_mode_copies_with_structure_and_no_llm():
    d, doc, slp = drafter()
    run = await draft_all(d, None)
    drafts = d.build_drafts(run, {})
    process = _claims(drafts, "TGT-6-CONTENT")
    kinds = [(c.kind.value, c.text[:30]) for c in process]
    assert kinds[:3] == [("paragraph", "The Process Owner must record "), ("paragraph", "Never close a deviation withou"),
                         ("heading", "Assessment")]
    assert ("bullet", "Deleting a record") in kinds and ("step", "QA may extend the deadline as ") in kinds
    assert ("figure", "[Figure]") in kinds and ("caption", "Image 1: The deviation flow") in kinds
    assert process[0].text.endswith("(see {{ref:SRC-5}}).") and not process[0].rule_ids_applied
    warning = next(c for c in process if c.text.startswith("Never close"))
    assert warning.callout_kind.value == "attention"  # from the slot plan's rule callout; drafted once, in place
    assert not any(s.slot_id == "TGT-6-CALLOUT_ATTENTION" and s.claims for x in drafts for s in x.slots)
    assert [c.text for c in process if c.kind == ClaimKind.HEADING] == ["Assessment", "Closure"]
    assert _claims(drafts, "TGT-3-ABBREVIATIONS")[0].kind == ClaimKind.TABLE_ROW
    assert _claims(drafts, "TGT-10-ENTRIES")[0].text == "1.0 | First version | J. Doe"
    assert [c.text for c in _claims(drafts, "TGT-3-TERMS")] == [GAP_TEXT]  # required, nothing in the source
    assert _claims(drafts, "TGT-3-TERMS")[0].is_gap_marker
    assert sum(c.is_gap_marker for x in drafts for c in x.iter_claims()) == 1
    assert all(c.source_unit_ids or c.is_gap_marker or c.kind == ClaimKind.HEADING for x in drafts for c in x.iter_claims())

    issues, coverage = draft_issues(drafts, slp, doc, template(), d.facts)
    assert coverage == 1.0 and [i for i in issues if i.gate] == []
    shared = [i.message for i in issues if "whole passage is copied" in i.message]
    assert len(shared) == 3  # roles, units and geography share SRC-2-U001; without an LLM each gets it whole


_SCOPE = ("This procedure is binding for all employees and contractors working in the Quality division and the "
          "Logistics department, independent of the country or site.")
_ROLES, _UNITS, _GEO = ("This procedure is binding for all employees and contractors",
                        "working in the Quality division and the Logistics department",
                        "independent of the country or site.")


def splitter(pieces):
    """A split fake: the same pieces for every request; ``pieces`` is [(slot ref, text)]."""
    def respond(prompt, n):
        return SplitOutput(splits=[Split(request_id=r, pieces=[SplitPiece(slot_ref=ref, text=t) for ref, t in pieces])
                                   for r in re.findall(r"^(R\d+): passage", prompt, re.M)])
    return FakeChain(respond)


_CLEAN = [("APPLICABILITY.roles", _ROLES), ("APPLICABILITY.units", _UNITS + ","), ("APPLICABILITY.geography", _GEO)]
_REFS = ["APPLICABILITY.roles", "APPLICABILITY.units", "APPLICABILITY.geography"]


def test_split_passage_accepts_only_the_whole_passage_in_order():
    cut = split_passage(_SCOPE, _CLEAN, _REFS)
    assert {ref: [_SCOPE[a:b] for a, b in spans] for ref, spans in cut.items()} == {
        "APPLICABILITY.roles": [_ROLES], "APPLICABILITY.units": [_UNITS], "APPLICABILITY.geography": [_GEO]}
    roles, units, geo = _CLEAN
    assert split_passage(_SCOPE, [roles, (units[0], "working in the Quality division"), geo], _REFS) is None  # words dropped
    assert split_passage(_SCOPE, [roles, (units[0], _ROLES), geo], _REFS) is None                            # repeated
    assert split_passage(_SCOPE, [roles, geo, units], _REFS) is None                                        # out of order
    assert split_passage(_SCOPE, [roles, (units[0], units[1] + " " + geo[1])], _REFS) is None               # a slot gets nothing
    assert split_passage(_SCOPE, [roles, ("APPLICABILITY.processes", units[1]), geo], _REFS) is None        # not its slot
    assert split_passage(_SCOPE, [roles, (units[0], units[1].upper()), geo], _REFS) is None                 # not verbatim
    two = split_passage(_SCOPE, [(roles[0], "This procedure is binding"), (roles[0], "for all employees and contractors"),
                                 units, geo], _REFS)
    assert two["APPLICABILITY.roles"] == [(0, len(_ROLES))]  # consecutive pieces of one slot merge


async def test_a_shared_passage_is_split_between_its_slots():
    d, doc, slp = drafter()
    chain = splitter(_CLEAN)
    run = await draft_all(d, FakeFactory(SplitOutput=chain))
    drafts = d.build_drafts(run, {})
    assert [c.text for c in _claims(drafts, "TGT-2-ROLES")] == [_ROLES]
    assert [c.text for c in _claims(drafts, "TGT-2-UNITS")] == [_UNITS]
    assert [c.text for c in _claims(drafts, "TGT-2-GEOGRAPHY")] == [_GEO]
    units = _claims(drafts, "TGT-2-UNITS")[0]
    assert [(s.unit_id, _SCOPE[s.start:s.end]) for s in units.spans] == [("SRC-2-U001", _UNITS)]
    prompt = chain.calls[0][1].content
    assert prompt.count("passage [SRC-2-U001]") == 1 and all(f"- {r}:" in prompt for r in _REFS)
    assert run.usage["calls"] == 1 and not any(c.rule_ids_applied for x in drafts for c in x.iter_claims())
    assert not [n for x in drafts for n in x.unresolved_items if "SRC-2-U001" in n]
    issues, coverage = draft_issues(drafts, slp, doc, template(), d.facts)
    assert coverage == 1.0 and [i.message for i in issues if i.gate] == []

    # A repair rebuilds the section from the saved drafts: each slot's piece is its part again, not the passage.
    again = d.prepare()
    d.restore(again, drafts)
    piece = again.job("TGT-2-UNITS").pieces[0]
    assert (piece.text, piece.mode) == (_UNITS, "copy")


async def test_a_split_that_loses_or_repeats_words_gives_each_slot_the_whole_passage():
    d, doc, slp = drafter()
    roles, units, geo = _CLEAN
    run = await draft_all(d, FakeFactory(SplitOutput=splitter([roles, (units[0], _ROLES), geo])))
    drafts = d.build_drafts(run, {})
    assert {_claims(drafts, f"TGT-2-{k}")[0].text for k in ("ROLES", "UNITS", "GEOGRAPHY")} == {_SCOPE}
    notes = [n for x in drafts for n in x.unresolved_items if "SRC-2-U001" in n]
    assert len(notes) == 1 and "could not be split" in notes[0]
    issues, coverage = draft_issues(drafts, slp, doc, template(), d.facts)
    assert coverage == 1.0 and [i.message for i in issues if i.gate] == []


async def test_narrative_and_figures_of_a_table_section_come_after_the_rows():
    from app.services.migration_v2.quality.validator import order_issues
    from tests.test_v2_slot_planner import sop as table_sop

    d, doc, slp = drafter(doc=table_sop())
    drafts = d.build_drafts(await draft_all(d, None), {})
    abbr = _claims(drafts, "TGT-3-ABBREVIATIONS")
    assert [c.kind.value for c in abbr] == ["table_row", "table_row", "figure", "caption"]  # below the tables
    assert abbr[-1].text == "Image 1: The deviation landscape"
    assert order_issues(drafts, doc, template(), slp) == []
    assert order_issues(drafts, doc, template())  # without the plan's below_unit_ids it would look reordered
    # A repair rebuild keeps them below.
    again = d.prepare()
    d.restore(again, drafts)
    rebuilt = d.build_drafts(again, {})
    assert [c.kind.value for c in _claims(rebuilt, "TGT-3-ABBREVIATIONS")] == ["table_row", "table_row", "figure", "caption"]


async def test_with_a_gwp_each_slot_rewrites_only_its_part():
    d, doc, slp = drafter(gwp())
    rewrite = FakeChain(echo())
    run = await draft_all(d, FakeFactory(SplitOutput=splitter(_CLEAN), RewriteOutput=rewrite))
    drafts = d.build_drafts(run, {}, model="azure/fake")
    prompt = "\n".join(m[1].content for m in rewrite.calls)
    assert f"[SRC-2-U001] (paragraph) {_UNITS}" in prompt and "(part:" not in prompt
    assert [c.text for c in _claims(drafts, "TGT-2-GEOGRAPHY")] == [_GEO]
    assert _claims(drafts, "TGT-2-ROLES")[0].spans and _claims(drafts, "TGT-2-ROLES")[0].rule_ids_applied == ["STY-001"]
    issues, coverage = draft_issues(drafts, slp, doc, template(), d.facts)
    assert coverage == 1.0 and [i.message for i in issues if i.gate] == []


# ── GWP rewrite ───────────────────────────────────────────────────────


async def test_rewrite_uses_the_jobs_rules_and_keeps_tables_figures_and_headings():
    d, doc, slp = drafter(gwp())
    chain = FakeChain(echo(lambda u, t: t.replace("This SOP describes", "This SOP explains")))
    run = await draft_all(d, FakeFactory(RewriteOutput=chain))
    drafts = d.build_drafts(run, {}, model="azure/fake")
    prompt = chain.calls[0][1].content
    assert "STY-001: Use active voice." in prompt and "STY-002: Keep scope statements short." in prompt
    assert "PRES-004" in prompt and "STR-001" not in prompt and "FMT-001" not in prompt  # placement and layout: not here
    assert "ROLE NAMES (keep exactly): " in prompt and "{{ref:SRC-5}} points to ROLES" in prompt
    assert "[SRC-3-U001]" not in prompt and "[Figure]" not in prompt  # tables and figures are never rewritten
    what = _claims(drafts, "TGT-1-WHAT")[0]
    assert what.text == "This SOP explains how deviations are recorded and assessed." and what.rule_ids_applied == ["STY-001"]
    assert _claims(drafts, "TGT-3-ABBREVIATIONS")[0].rule_ids_applied == []
    assert [c.text for c in _claims(drafts, "TGT-6-CONTENT") if c.kind == ClaimKind.HEADING] == ["Assessment", "Closure"]
    assert drafts[0].prompt_version == DRAFTER_PROMPT_VERSION and drafts[0].model == "azure/fake"
    issues, coverage = draft_issues(drafts, slp, doc, template(), d.facts)
    assert coverage == 1.0 and [i for i in issues if i.gate] == []


async def test_failed_passages_get_one_targeted_retry_then_are_copied():
    d, doc, slp = drafter(gwp())

    def respond(prompt, n):
        out = echo()(prompt, n)
        for slot in out.slots:
            for c in slot.claims:
                if c.source_unit_ids == ["SRC-6-U001"]:
                    c.text = c.text.replace("5 days", "10 days").replace("{{ref:SRC-5}}", "section 5")  # number and ref lost
                if c.source_unit_ids == ["SRC-6.1-U003"]:
                    c.text = "QA must extend the deadline as {{ref:SRC-6.2}}." if n == 1 else c.text  # fixed on retry
            slot.claims = [c for c in slot.claims if c.source_unit_ids != ["SRC-1-U002"] or n > 1]  # skipped, then given
        return out

    chain = FakeChain(respond)
    run = await draft_all(d, FakeFactory(RewriteOutput=chain))
    drafts = d.build_drafts(run, {})
    retry = [c[1].content for c in chain.calls if "SECOND ATTEMPT" in c[1].content]
    assert retry, "the failed passages get a second, targeted call"
    assert "SRC-1-U002: not cited by any claim" in retry[0] and "[SRC-6.1-U003]" in retry[0]
    assert "[SRC-1-U001]" not in retry[0]  # passages that passed are not sent again
    record = next(c for c in _claims(drafts, "TGT-6-CONTENT") if c.source_unit_ids == ["SRC-6-U001"])
    assert record.text == "The Process Owner must record every deviation within 5 days (see {{ref:SRC-5}})."  # copied
    assert not record.rule_ids_applied
    extend = next(c for c in _claims(drafts, "TGT-6-CONTENT") if c.source_unit_ids == ["SRC-6.1-U003"])
    assert extend.text == "QA may extend the deadline as {{ref:SRC-6.2}}." and extend.rule_ids_applied == ["STY-001"]
    notes = " | ".join(n for x in drafts for n in x.unresolved_items)
    assert "PROCESS.content: 1 passage(s) copied verbatim" in notes and "SRC-6-U001" in notes
    issues, coverage = draft_issues(drafts, slp, doc, template(), d.facts)
    assert coverage == 1.0 and [i for i in issues if i.gate] == []


async def test_a_dropped_quoted_title_and_a_failed_call_fall_back_to_the_source():
    d, doc, slp = drafter(gwp())
    chain = FakeChain(lambda prompt, n: RuntimeError("timeout") if n <= 2 else echo(
        lambda u, t: "Refer to the closure guideline." if u == "SRC-6.2-U001" else t)(prompt, n))
    run = await draft_all(d, FakeFactory(RewriteOutput=chain))
    drafts = d.build_drafts(run, {})
    closure = next(c for c in _claims(drafts, "TGT-6-CONTENT") if c.source_unit_ids == ["SRC-6.2-U001"])
    assert "“Deviation Closure Guideline”" in closure.text  # verbatim, after both attempts failed
    issues, _ = draft_issues(drafts, slp, doc, template(), d.facts)
    assert [i for i in issues if i.gate] == []


# ── Draft checks ──────────────────────────────────────────────────────


async def test_draft_checks_catch_broken_drafts():
    d, doc, slp = drafter()
    drafts = d.build_drafts(await draft_all(d, None), {})
    broken = [x.model_copy(deep=True) for x in drafts]
    process = next(s for x in broken for s in x.slots if s.slot_id == "TGT-6-CONTENT")
    process.claims[0].text = "The Process Owner must record every deviation within 5 days."  # token lost
    process.claims[1].source_unit_ids = ["SRC-1-U001"]  # cites a passage of another slot
    process.claims.append(Claim(claim_id="C-X", text="Extra {{ref:SRC-9}}.", source_unit_ids=["SRC-6-U001"]))
    process.claims.append(Claim(claim_id="C-G", text=GAP_TEXT, is_gap_marker=True))
    terms = next(x for x in broken if x.target_section_id == "TGT-3")
    terms.slots = [s for s in terms.slots if s.slot_id != "TGT-3-TERMS"]  # its gap marker is gone
    issues, coverage = draft_issues(broken, slp, doc, template(), d.facts)
    gates = {(i.gate, i.slot_id) for i in issues if i.gate}
    assert (Gate.BROKEN_CROSS_REFERENCE, "TGT-6-CONTENT") in gates
    assert (Gate.UNSUPPORTED_CLAIM, "TGT-6-CONTENT") in gates
    assert (Gate.UNACCOUNTED_SOURCE, "TGT-6-CONTENT") in gates  # the warning passage is no longer cited
    assert (Gate.MISSING_SLOT, "TGT-3-TERMS") in gates
    assert coverage < 1.0


# ── Stage, orchestrator and API ───────────────────────────────────────

from tests.test_v2_migration_jobs import END_STATUS, Env, _events, _wait_for  # noqa: E402


async def test_stage_writes_versioned_drafts_and_checks(tmp_path):
    env = Env(tmp_path)
    orch = env.orchestrator()
    job = await orch.wait(env.create(orch).job_id)
    drafts = [orch.artifacts.latest(job, ArtifactKind.SECTION_DRAFT, SectionDraft, s.target_section_id) for s in job.sections]
    assert all(d is not None and d.version == 1 for d in drafts)
    claims = [c for d in drafts for c in d.iter_claims()]
    assert claims and all(c.source_unit_ids or c.is_gap_marker or c.kind == ClaimKind.HEADING for c in claims)
    report = orch.artifacts.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport, "drafts")
    assert report.unit_coverage == 1.0 and report.open_gate_issues == []
    event = _events(env.store, job.job_id, "drafter")[0]["detail"]
    assert (event["mode"], event["llm"], event["gaps"]) == ("placement", False, sum(c.is_gap_marker for c in claims))
    assert any("{{ref:SRC-5.1}}" in c.text for c in claims)  # the example's "see Section 5.1"


async def test_stage_rewrites_with_the_named_guides_style_rules(tmp_path):
    env = Env(tmp_path)
    guide = env.approved_guide("GWP")
    rules = GwpRuleSet.model_validate_json(Path(guide["rules_path"]).read_text(encoding="utf-8"))
    style = GwpRule(rule_id="STY-042", category=RuleCategory.STYLE, text="Start each step with a verb.",
                    status=RuleStatus.APPROVED)
    Path(guide["rules_path"]).write_text(rules.model_copy(update={"rules": rules.rules + [style]}).model_dump_json(),
                                         encoding="utf-8")
    chain = FakeChain(echo(rules=("STY-042",)))
    orch = env.orchestrator()
    orch.chain_factory = FakeFactory(RewriteOutput=chain)
    job = await orch.wait(env.create(orch, gwp_id="GWP").job_id)
    assert "STY-042: Start each step with a verb." in chain.calls[0][1].content
    drafts = [orch.artifacts.latest(job, ArtifactKind.SECTION_DRAFT, SectionDraft, s.target_section_id) for s in job.sections]
    assert any("STY-042" in c.rule_ids_applied for d in drafts for c in d.iter_claims())
    event = _events(env.store, job.job_id, "drafter")[0]["detail"]
    assert event["mode"] == "gwp" and event["llm"] and event["usage"]["calls"] >= 1


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


def test_api_reads_and_edits_drafts(api):
    client, orch, env = api
    job_id = client.post("/api/v1/migrations", json={"sop_record_id": env.sop["id"], "template_id": "TPL",
                                                     "mode": "auto"}).json()["job_id"]
    _wait_for(client, job_id, {END_STATUS.value})
    drafts = client.get(f"/api/v1/migrations/{job_id}/drafts").json()
    assert drafts and all(d["version"] == 1 for d in drafts)
    section = drafts[0]["target_section_id"]
    draft = client.get(f"/api/v1/migrations/{job_id}/sections/{section}/draft").json()
    slot = next(s for s in draft["slots"] if s["claims"] and not s["claims"][0].get("is_gap_marker"))
    assert client.get(f"/api/v1/migrations/{job_id}/drafts/validation").json()["unit_coverage"] == 1.0

    claims = slot["claims"]
    edited = [{**claims[0], "text": "Edited by the reviewer."}] + claims[1:]
    saved = client.patch(f"/api/v1/migrations/{job_id}/sections/{section}/slots/{slot['slot_id']}", json={"claims": edited})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["version"], body["origin"]) == (2, "human") and "validation" in body
    assert client.get(f"/api/v1/migrations/{job_id}").json()["status"] == "MANUALLY_EDITED"

    uncited = [{"claim_id": "C-NEW", "text": "Nothing cited."}]
    assert client.patch(f"/api/v1/migrations/{job_id}/sections/{section}/slots/{slot['slot_id']}",
                        json={"claims": uncited}).status_code == 422  # the schema still demands citations
    twice = [claims[0], claims[0]]
    assert client.patch(f"/api/v1/migrations/{job_id}/sections/{section}/slots/{slot['slot_id']}",
                        json={"claims": twice}).status_code == 422
    assert client.patch(f"/api/v1/migrations/{job_id}/sections/{section}/slots/NOPE",
                        json={"claims": edited}).status_code == 404


# ── Samples in placement mode vs the goldens ──────────────────────────

GOLDEN_DIR = ROOT / "documents" / "golden"


async def test_samples_placement_drafts_keep_everything(samples):  # noqa: F811
    from app.services.migration_v2.inspection.golden import draft_heading_problems, draft_missing_must_preserve
    from app.services.migration_v2.planning.section_planner import SectionPlanner

    tpl, docs = samples
    assert docs
    for doc, golden in docs:
        sections = SectionPlanner(doc, tpl)
        sp = sections.build_plan(sections.propose(), "J", 1)
        slots = SlotPlanner(doc, tpl, sp)
        slp = slots.build_plan(slots.propose(), "J", 1)
        facts = extract_facts(doc, "J")
        d = Drafter(doc, tpl, sp, slp, facts)
        drafts = d.build_drafts(await draft_all(d, None), {})
        issues, coverage = draft_issues(drafts, slp, doc, tpl, facts)
        assert coverage == 1.0, golden["sop_file"]
        assert [i.message for i in issues if i.gate] == [], golden["sop_file"]
        assert draft_heading_problems(drafts, golden) == [], golden["sop_file"]
        assert draft_missing_must_preserve(drafts, doc, golden) == [], golden["sop_file"]


from tests.test_v2_slot_planner import samples  # noqa: E402,F401  (fixture)

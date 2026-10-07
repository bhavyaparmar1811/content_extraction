"""Phase 7: section planner — name and content signals, rule proposal, LLM confirm/correct, validator, stage, API.

The real-sample tests run the planner with the LLM off and compare the plans with the reviewed
golden section mappings (``documents/golden``). The live LLM check is in
``test_v2_section_planner_live.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.v2 import (
    ArtifactKind,
    ContentType,
    Gate,
    JobMode,
    JobStatus,
    MappingOrigin,
    MappingStatus,
    MappingType,
    SectionMapping,
    SectionPlan,
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
from app.services.migration_v2.planning.section_planner import (
    SectionChange,
    SectionFlag,
    SectionPlanCorrections,
    SectionPlanner,
    run_confirm,
)
from app.services.llm.prompts.v2.section_planner import PROMPT_VERSION as SECTION_PLANNER_PROMPT_VERSION
from app.services.migration_v2.planning.section_validator import validate_section_plan
from app.services.migration_v2.inspection.golden import golden_problems
from app.services.migration_v2.planning.signals import name_score, profile, unit_labels

ROOT = Path(__file__).resolve().parent.parent


# ── Synthetic template and SOP ────────────────────────────────────────

_TARGETS = [
    ("PURPOSE", "PURPOSE", True, [("what", "purpose", "Brief description of what the document is about.")]),
    ("APPLICABILITY", "APPLICABILITY", True, [("roles", "scope", "Explain which target roles must follow this document.")]),
    ("DEFINITIONS", "DEFINITIONS & ABBREVIATIONS", True, [("terms", "definition", "Add definitions and abbreviations.")]),
    ("ROLES", "ROLES & RESPONSIBILITIES", True, [("roles", "responsibility", "List all roles that have responsibilities.")]),
    ("PROCESS", "PROCESS", True, [("content", "ordered_procedure", "High level description of the process steps."),
                                  ("callout_attention", "warning", "Attention box.")]),
    ("ASSOCIATED_DOCUMENTS", "ASSOCIATED DOCUMENTS", True, [("documents", "reference", "List of associated documents.")]),
    ("REFERENCES", "REFERENCES", True, [("documents", "reference", "Internal or external documents supporting the subject.")]),
    ("DISTRIBUTION", "distribution of controlled prints/copies", False, [("content", "supporting_information", "Distribution.")]),
    ("DOCUMENT_HISTORY", "DOCUMENT HISTORY", True, [("entries", "record", "Fill the table: Version | Description | Author")]),
]


def template(aliases: dict[str, list[str]] | None = None) -> TemplateModel:
    sections = []
    for i, (key, heading, required, slots) in enumerate(_TARGETS, start=1):
        sid = f"TGT-{i}"
        sections.append(TargetSection(
            section_id=sid, key=key, number=str(i), heading=heading, level=1, display_order=i, required=required,
            aliases=(aliases or {}).get(key, []),
            slots=[TargetSlot(slot_id=f"{sid}-{k.upper()}", section_id=sid, key=k, instruction=ins,
                              content_type=ContentType(ct), display_order=j) for j, (k, ct, ins) in enumerate(slots)],
        ))
    return TemplateModel(template_id="TPL", template_version=1, source_file="t.docx", sections=sections)


class _Doc:
    def __init__(self):
        self.sections: list[SourceSection] = []
        self.seq = 0

    def section(self, sid, number, heading, parent=None, units=(), boilerplate=False):
        out = []
        for i, u in enumerate(units, start=1):
            kind, text, *extra = u if isinstance(u, tuple) else ("paragraph", u)
            table_ref = None
            if kind == "row":
                headers, cells = extra
                table_ref = TableRef(table_id=f"{sid}-T1", row_index=i, header_cells=headers,
                                     cells=[TableCell(col=c, text=t) for c, t in enumerate(cells)])
                kind = "table_row"
            out.append(SourceUnit(unit_id=f"{sid}-U{i:03d}", content_hash=f"{sid}{i}", section_id=sid, seq=self.seq,
                                  unit_type=UnitType(kind), text=text, table_ref=table_ref, is_boilerplate=boilerplate))
            self.seq += 1
        level = 0 if number == "0" else (number.count(".") + 1 if number else 1)
        self.sections.append(SourceSection(section_id=sid, number=number, heading=heading, level=level,
                                           section_order=len(self.sections), parent_id=parent, units=out))
        return self

    def build(self) -> SourceDocument:
        return SourceDocument(document_id="SOP", source_file="sop.docx", file_type="docx", sections=self.sections)


def sop() -> SourceDocument:
    d = _Doc()
    d.section("SRC-0", "0", "PREAMBLE", units=[("row", "Title | Deviation handling", ["Field", "Value"], ["Title", "Deviation handling"])],
              boilerplate=True)
    d.section("SRC-1", "1", "PURPOSE", units=["This SOP describes how deviations are handled.",
                                              "It aims to keep deviations under control."])
    d.section("SRC-2", "2", "SCOPE", units=["This SOP is binding for all employees at all sites world-wide.",
                                            "It applies to the Quality division."])
    d.section("SRC-2.1", "2.1", "Prohibited uses", parent="SRC-2", units=[
        "The following activities must not be performed with the tool:",
        ("bullet", "Placing an electronic signature"), ("bullet", "Bypassing the audit trail"),
        ("bullet", "Approving releases"),
    ])
    d.section("SRC-3", "3", "Abbreviations", units=[
        ("row", "QA | Quality Assurance", ["Abbreviation", "Description"], ["QA", "Quality Assurance"]),
        ("row", "SOP | Standard Operating Procedure", ["Abbreviation", "Description"], ["SOP", "Standard Operating Procedure"]),
    ])
    d.section("SRC-4", "4", "Definitions", units=[
        ("row", "Deviation | A departure from an approved instruction", ["Term", "Definition"], ["Deviation", "A departure"]),
    ])
    d.section("SRC-5", "5", "ROLES & RESPONSIBILITIES", units=[
        ("row", "Process Owner | Owns the process", ["Role", "Responsibility"], ["Process Owner", "Owns the process"]),
        ("row", "QA | Approves deviations", ["Role", "Responsibility"], ["QA", "Approves deviations"]),
    ])
    d.section("SRC-6", "6", "PROCEDURE", units=["The Process Owner must record every deviation."])
    d.section("SRC-6.1", "6.1", "Step 1: Record", parent="SRC-6", units=[
        ("procedure_step", "Record the deviation in the system."), ("procedure_step", "Notify QA within 2 days.")])
    d.section("SRC-6.2", "6.2", "Step 2: Assess", parent="SRC-6", units=[
        ("procedure_step", "Assess the impact."), ("procedure_step", "QA must approve the assessment.")])
    d.section("SRC-7", "7", "Attachments", units=[("row", "1 | FRM-QA-001 | Deviation form", ["No.", "Name", "Title"],
                                                   ["1", "FRM-QA-001", "Deviation form"])])
    d.section("SRC-8", "8", "DOCUMENT HISTORY", units=[
        ("row", "1.0 | First version | J. Doe", ["Version", "Description of Changes", "Author"], ["1.0", "First version", "J. Doe"])],
        boilerplate=True)
    return d.build()


def _by_target(plan: SectionPlan) -> dict[str, SectionMapping]:
    return {m.target_section_id: m for m in plan.mappings if m.target_section_id}


# ── Signals ───────────────────────────────────────────────────────────


def test_name_scores_use_headings_aliases_and_config():
    tpl = template({"PURPOSE": ["Zweck"]})
    t = {s.key: s for s in tpl.sections}
    assert name_score("1 PURPOSE", t["PURPOSE"])[0] == 1.0
    assert name_score("SCOPE", t["APPLICABILITY"]) == (1.0, "scope")
    assert name_score("Zweck", t["PURPOSE"])[0] == 1.0  # template-config alias
    assert name_score("PROCEDURE / Process", t["PROCESS"])[0] == 1.0
    assoc = name_score("Associated Reference DOCUMENTS (RD)", t["ASSOCIATED_DOCUMENTS"])[0]
    refs = name_score("Associated Reference DOCUMENTS (RD)", t["REFERENCES"])[0]
    assert assoc > refs
    assert name_score("Risk based approach", t["PURPOSE"])[0] == 0.0


def test_content_labels():
    doc = sop()
    units = {u.unit_id: u for u in doc.iter_units()}
    assert max(unit_labels(units["SRC-3-U001"]), key=unit_labels(units["SRC-3-U001"]).get) == "definition"
    assert max(unit_labels(units["SRC-5-U001"]), key=unit_labels(units["SRC-5-U001"]).get) == "responsibility"
    assert max(unit_labels(units["SRC-7-U001"]), key=unit_labels(units["SRC-7-U001"]).get) == "reference"
    assert "purpose" in unit_labels(units["SRC-1-U001"])
    assert "scope" in unit_labels(units["SRC-2-U001"])
    # Bullets under "...must not be performed:" inherit the restriction.
    prohibited = profile([u for u in doc.iter_units() if u.section_id == "SRC-2.1"])
    assert max(prohibited, key=prohibited.get) == "restriction" and prohibited["restriction"] > 0.6


# ── Rule proposal ─────────────────────────────────────────────────────


def test_rule_plan_maps_by_name_and_content():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    plan = planner.build_plan(planner.propose(), "J", 1)
    m = _by_target(plan)
    ids = {t.key: t.section_id for t in tpl.sections}

    assert (m[ids["PURPOSE"]].source_section_ids, m[ids["PURPOSE"]].mapping_type) == (["SRC-1"], MappingType.ONE_TO_ONE)
    # SCOPE → APPLICABILITY by alias; its prohibited-uses subsection moves to PROCESS on content: a split.
    app, proc = m[ids["APPLICABILITY"]], m[ids["PROCESS"]]
    assert app.mapping_type == MappingType.SPLIT and app.source_section_ids == ["SRC-2"]
    assert set(app.unit_ids) == {"SRC-2-U001", "SRC-2-U002"}
    assert proc.mapping_type == MappingType.SPLIT and proc.source_section_ids == ["SRC-2.1", "SRC-6", "SRC-6.1", "SRC-6.2"]
    assert set(proc.unit_ids) == {f"SRC-2.1-U00{i}" for i in range(1, 5)}
    # Two source chapters into one target: a merge, in source order.
    assert (m[ids["DEFINITIONS"]].source_section_ids, m[ids["DEFINITIONS"]].mapping_type) == (["SRC-3", "SRC-4"], MappingType.MERGE)
    assert m[ids["ROLES"]].source_section_ids == ["SRC-5"]
    assert m[ids["ASSOCIATED_DOCUMENTS"]].source_section_ids == ["SRC-7"]  # "Attachments" alias
    assert m[ids["DOCUMENT_HISTORY"]].source_section_ids == ["SRC-8"]
    # Gaps: a required target with no source is flagged; an optional one is removed.
    assert m[ids["REFERENCES"]].status == MappingStatus.SOURCE_CONTENT_NOT_FOUND
    assert m[ids["DISTRIBUTION"]].status == MappingStatus.NOT_APPLICABLE
    omits = [x for x in plan.mappings if x.mapping_type == MappingType.OMIT]
    assert [x.source_section_ids for x in omits] == [["SRC-0"]] and omits[0].justification
    assert all(x.origin == MappingOrigin.RULE for x in plan.mappings)

    report = validate_section_plan(plan, doc, tpl)
    assert report.issues == [] and report.unit_coverage == 1.0


def test_unknown_section_is_unresolved_and_blocks_approval():
    d = _Doc()
    d.section("SRC-1", "1", "PURPOSE", units=["This SOP describes the process."])
    d.section("SRC-2", "2", "Miscellaneous", units=["Lorem ipsum dolor sit amet.", "Consectetur adipiscing elit."])
    doc, tpl = d.build(), template()
    planner = SectionPlanner(doc, tpl)
    plan = planner.build_plan(planner.propose(), "J", 1)
    unresolved = [x for x in plan.mappings if x.mapping_type == MappingType.UNRESOLVED and x.source_section_ids]
    assert [x.source_section_ids for x in unresolved] == [["SRC-2"]]
    gates = {i.gate for i in validate_section_plan(plan, doc, tpl).open_gate_issues}
    assert gates == {Gate.UNACCOUNTED_SOURCE}


# ── Validator ─────────────────────────────────────────────────────────


def test_validator_catches_plan_errors():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    plan = planner.build_plan(planner.propose(), "J", 1)
    ids = {t.key: t.section_id for t in tpl.sections}

    # A required target dropped and its source unplaced.
    without_roles = plan.model_copy(update={"mappings": [x for x in plan.mappings if x.target_section_id != ids["ROLES"]]})
    gates = {i.gate for i in validate_section_plan(without_roles, doc, tpl).open_gate_issues}
    assert gates == {Gate.MISSING_SECTION, Gate.UNACCOUNTED_SOURCE}

    # Out of source order; and a unit placed twice.
    swapped = [x.model_copy(update={"source_section_ids": ["SRC-4", "SRC-3"]}) if x.target_section_id == ids["DEFINITIONS"] else x
               for x in plan.mappings]
    swapped.append(SectionMapping(target_section_id=ids["PURPOSE"], source_section_ids=["SRC-7"],
                                  mapping_type=MappingType.ONE_TO_ONE))
    report = validate_section_plan(plan.model_copy(update={"mappings": swapped}), doc, tpl)
    assert Gate.SEQUENCE_VIOLATION in {i.gate for i in report.issues}
    assert any("more than one target" in i.message and i.unit_ids == ["SRC-7-U001"] for i in report.issues)

    # Unknown IDs.
    bogus = plan.model_copy(update={"mappings": plan.mappings + [SectionMapping(
        target_section_id="TGT-99", source_section_ids=["SRC-1"], mapping_type=MappingType.ONE_TO_ONE)]})
    assert any("unknown target section" in i.message for i in validate_section_plan(bogus, doc, tpl).issues)


# ── LLM confirm / correct (mocked) ────────────────────────────────────


class FakeChain:
    def __init__(self, *outputs, usage=None):
        self.outputs = list(outputs)
        self.calls = []
        self.usage = usage or {"input_tokens": 1200, "output_tokens": 40, "total_tokens": 1240}

    async def ainvoke(self, messages):
        self.calls.append(messages)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"raw": SimpleNamespace(usage_metadata=self.usage), "parsed": out, "parsing_error": None}


def _close_call_sop() -> SourceDocument:
    """'Associated Reference Documents' is a close call between two targets: the rules flag it [CHECK]."""
    d = _Doc()
    d.section("SRC-1", "1", "PURPOSE", units=["This SOP describes the process."])
    d.section("SRC-2", "2", "PROCEDURE", units=[("procedure_step", "Record the deviation."), ("procedure_step", "Assess it.")])
    d.section("SRC-3", "3", "Associated Reference DOCUMENTS (RD)", units=[
        ("row", "1 | BI-VQD-1-RD00 | List of reference documents", ["No.", "Name", "Title"], ["1", "BI-VQD-1-RD00", "List"])])
    return d.build()


def test_prompt_is_compact_and_previews_only_doubts():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    text = planner.prompt(proposal, preview_chars=40)
    assert "SRC-2.1 | Prohibited uses | 4u" in text and "[MOVED away from parent's APPLICABILITY]" in text
    preview = next(line for line in text.splitlines() if line.startswith("SRC-2.1-U001: "))
    assert preview.endswith("…") and len(preview) == len("SRC-2.1-U001: ") + 40 + 1  # previews are truncated
    assert "SRC-1-U001" not in text  # confident sections get no previews
    assert "STRUCTURAL WRITING RULES" not in text  # no GWP, no rules
    assert len(text) < 3000


async def test_llm_confirms_all_and_clears_doubts():
    doc, tpl = _close_call_sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    assert any(d.check for d in proposal.decisions.values())
    rules_only = planner.build_plan(planner.propose(), "J", 1)
    assert any(x.status == MappingStatus.NEEDS_REVIEW for x in rules_only.mappings)

    chain = FakeChain(SectionPlanCorrections())
    result = await run_confirm(planner, proposal, chain, planner.prompt(proposal))
    planner.apply(proposal, result.corrections)
    plan = planner.build_plan(proposal, "J", 1)
    assert all(x.status != MappingStatus.NEEDS_REVIEW for x in plan.mappings)
    assert result.usage == {"calls": 1, "input_tokens": 1200, "output_tokens": 40, "total_tokens": 1240}
    assert "confirmed by LLM" in _by_target(plan)[tpl.sections[5].section_id].reason


async def test_llm_correction_moves_a_section_and_marks_its_origin():
    doc, tpl = _close_call_sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    chain = FakeChain(SectionPlanCorrections(changes=[SectionChange(
        source_section_ids=["SRC-3"], target_key="REFERENCES", mapping_type=MappingType.ONE_TO_ONE,
        reason="it lists reference documents")]))
    result = await run_confirm(planner, proposal, chain, planner.prompt(proposal))
    planner.apply(proposal, result.corrections)
    plan = planner.build_plan(proposal, "J", 1)
    refs = _by_target(plan)["TGT-7"]
    assert refs.source_section_ids == ["SRC-3"] and refs.origin == MappingOrigin.LLM and "LLM:" in refs.reason
    assert _by_target(plan)["TGT-6"].status == MappingStatus.SOURCE_CONTENT_NOT_FOUND
    assert _by_target(plan)["TGT-5"].origin == MappingOrigin.RULE


async def test_llm_unit_level_split():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    # Move one SCOPE paragraph to PURPOSE.
    chain = FakeChain(SectionPlanCorrections(changes=[SectionChange(
        source_section_ids=["SRC-2"], target_key="PURPOSE", mapping_type=MappingType.SPLIT,
        unit_ids=["SRC-2-U002"], reason="states the aim")]))
    result = await run_confirm(planner, proposal, chain, planner.prompt(proposal))
    planner.apply(proposal, result.corrections)
    plan = planner.build_plan(proposal, "J", 1)
    m = _by_target(plan)
    assert m["TGT-1"].mapping_type == MappingType.SPLIT and m["TGT-1"].unit_ids == ["SRC-2-U002"]
    assert "SRC-2-U002" not in m["TGT-2"].unit_ids and "SRC-2-U001" in m["TGT-2"].unit_ids
    assert validate_section_plan(plan, doc, tpl).open_gate_issues == []


async def test_invalid_corrections_get_one_repair_then_are_dropped():
    doc, tpl = _close_call_sop(), template()
    planner = SectionPlanner(doc, tpl)
    bad = SectionPlanCorrections(changes=[SectionChange(source_section_ids=["SRC-3"], target_key="LITERATURE",
                                                        mapping_type=MappingType.ONE_TO_ONE, reason="x")])
    good = SectionPlanCorrections(changes=[SectionChange(source_section_ids=["SRC-3"], target_key="REFERENCES",
                                                         mapping_type=MappingType.ONE_TO_ONE, reason="x")])
    proposal = planner.propose()
    chain = FakeChain(bad, good)
    result = await run_confirm(planner, proposal, chain, planner.prompt(proposal))
    assert len(chain.calls) == 2 and result.problems == [] and result.usage["calls"] == 2
    assert "unknown target_key 'LITERATURE'" in chain.calls[1][-1].content

    proposal = planner.propose()
    chain = FakeChain(bad, bad)
    result = await run_confirm(planner, proposal, chain, planner.prompt(proposal))
    assert result.corrections.changes == [] and result.problems


# ── Stage and API ─────────────────────────────────────────────────────

from tests.test_v2_migration_jobs import Env, _events, _wait_for  # noqa: E402


class FakeFactory:
    def __init__(self, chain):
        self.chain = chain
        self.kwargs = None

    def create_structured_planner(self, schema, include_raw=False):
        self.kwargs = {"schema": schema, "include_raw": include_raw}
        return self.chain

    def planner_label(self):
        return "azure/fake"


async def test_stage_records_llm_usage_and_falls_back_on_failure(tmp_path):
    env = Env(tmp_path)
    factory = FakeFactory(FakeChain(SectionPlanCorrections()))
    orch = env.orchestrator()
    orch.chain_factory = factory
    job = await orch.wait(env.create(orch).job_id)
    plan = orch.artifacts.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan)
    assert (plan.origin.value, plan.model, plan.prompt_version) == ("llm", "azure/fake", SECTION_PLANNER_PROMPT_VERSION)
    assert plan.token_usage["input_tokens"] == 1200 and factory.kwargs["include_raw"] is True
    assert _events(env.store, job.job_id, "section_planner_llm")[0]["detail"]["changes"] == 0

    orch.chain_factory = FakeFactory(FakeChain(RuntimeError("Azure timeout")))
    job = await orch.wait(env.create(orch).job_id)
    plan = orch.artifacts.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan)
    assert plan.origin.value == "rule" and plan.token_usage == {}
    assert "Azure timeout" in _events(env.store, job.job_id, "section_planner_llm_failed")[0]["detail"]["error"]
    assert job.status == JobStatus.COMPLETED_WITH_WARNINGS  # the rule plan carries on


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


def test_api_blocks_approval_while_the_plan_has_gate_issues(api):
    client, orch, env = api
    job_id = client.post("/api/v1/migrations", json={"sop_record_id": env.sop["id"], "template_id": "TPL"}).json()["job_id"]
    _wait_for(client, job_id, {"SECTION_PLAN_REVIEW_PENDING"})
    plan = client.get(f"/api/v1/migrations/{job_id}/section-plan").json()

    # A reviewer removes the only mapping: the source is unaccounted and the required section is missing.
    broken = {**plan, "mappings": []}
    saved = client.patch(f"/api/v1/migrations/{job_id}/section-plan", json=broken)
    assert saved.status_code == 200
    gates = {i.get("gate") for i in saved.json()["validation"]["issues"]}
    assert {"missing_section", "unaccounted_source"} <= gates
    refused = client.post(f"/api/v1/migrations/{job_id}/section-plan/approve")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "PLAN_HAS_GATE_ISSUES"

    fixed = client.patch(f"/api/v1/migrations/{job_id}/section-plan", json=plan)
    assert fixed.json()["validation"]["issues"] == []
    assert client.get(f"/api/v1/migrations/{job_id}/section-plan/validation").json()["issues"] == []
    assert client.post(f"/api/v1/migrations/{job_id}/section-plan/approve").status_code == 200

    # Mappings a reviewer changed are marked human; unchanged ones keep their origin.
    edited = client.get(f"/api/v1/migrations/{job_id}/section-plan").json()
    assert edited["mappings"][0]["origin"] == "human"  # re-added after the reviewer removed it


# ── Real samples vs the golden section mappings (LLM off) ─────────────

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


def test_samples_rule_plans_match_the_goldens(samples):
    tpl, docs = samples
    assert len(docs) == 3
    for doc, golden in docs:
        planner = SectionPlanner(doc, tpl)
        proposal = planner.propose()
        plan = planner.build_plan(proposal, "J", 1)
        assert golden_problems(plan, doc, tpl, golden) == [], golden["sop_file"]
        assert validate_section_plan(plan, doc, tpl).open_gate_issues == [], golden["sop_file"]
        assert len(planner.prompt(proposal)) < 6000  # about 1.5k tokens at most, plus the fixed instructions


def test_golden_comparison_catches_a_wrong_plan(samples):
    tpl, docs = samples
    doc, golden = next((d, g) for d, g in docs if "RPAS" in g["sop_file"])
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    acceptance = next(s for s in doc.sections if s.heading.startswith("Acceptance criteria"))
    proposal.decisions[acceptance.section_id] = proposal.decisions[acceptance.parent_id]  # keep it under SCOPE
    plan = planner.build_plan(proposal, "J", 1)
    problems = golden_problems(plan, doc, tpl, golden)
    assert len(problems) == 1 and "Acceptance criteria" in problems[0]


async def test_llm_cannot_move_or_omit_a_section_its_name_places():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    omit_purpose = SectionPlanCorrections(changes=[SectionChange(
        source_section_ids=["SRC-1"], target_key=None, mapping_type=MappingType.OMIT, reason="looks like other text")])
    chain = FakeChain(omit_purpose, omit_purpose)
    result = await run_confirm(planner, proposal, chain, planner.prompt(proposal))
    # A confident section: the invalid change is dropped without paying for a repair call.
    assert len(chain.calls) == 1 and "matches template section PURPOSE by name" in result.problems[0]
    planner.apply(proposal, result.corrections)
    plan = planner.build_plan(proposal, "J", 1)
    assert _by_target(plan)["TGT-1"].source_section_ids == ["SRC-1"]
    # Moving single units out of it is still allowed.
    split = SectionPlanCorrections(changes=[SectionChange(
        source_section_ids=["SRC-1"], target_key="PROCESS", mapping_type=MappingType.SPLIT, unit_ids=["SRC-1-U002"],
        reason="a process statement")])
    assert planner.change_problems(planner.propose(), split) == []


async def test_llm_flag_reaches_the_reviewer_without_changing_the_plan():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    flagged = SectionPlanCorrections(flags=[
        SectionFlag(source_section_ids=["SRC-6"], note="A responsibility statement may belong to ROLES."),
        SectionFlag(source_section_ids=["SRC-404"], note="unknown section"),
    ])
    result = await run_confirm(planner, proposal, FakeChain(flagged), planner.prompt(proposal))
    planner.apply(proposal, result.corrections)
    plan = planner.build_plan(proposal, "J", 1)
    process = _by_target(plan)["TGT-5"]
    # Same placement as the rules decided, but marked for review with the LLM's note.
    assert process.source_section_ids == ["SRC-2.1", "SRC-6", "SRC-6.1", "SRC-6.2"]
    assert process.status == MappingStatus.NEEDS_REVIEW and "LLM: A responsibility statement may belong to ROLES." in process.reason
    assert proposal.notes == ["flag on unknown section 'SRC-404' dropped"]
    report = validate_section_plan(plan, doc, tpl)
    assert report.open_gate_issues == []  # a flag never blocks approval
    assert any(i.target_section_id == "TGT-5" and "LLM: A responsibility" in i.message for i in report.issues)


async def test_flags_survive_when_invalid_changes_are_dropped():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    mixed = SectionPlanCorrections(
        changes=[SectionChange(source_section_ids=["SRC-6"], target_key="ROLES", mapping_type=MappingType.SPLIT,
                               unit_ids=["unit_id_responsibility"], reason="guessed")],
        flags=[SectionFlag(source_section_ids=["SRC-6"], note="May hold a responsibility statement.")],
    )
    result = await run_confirm(planner, proposal, FakeChain(mixed), planner.prompt(proposal))
    assert result.corrections.changes == [] and len(result.corrections.flags) == 1


async def test_a_move_with_guessed_unit_ids_becomes_a_flag():
    doc, tpl = sop(), template()
    planner = SectionPlanner(doc, tpl)
    proposal = planner.propose()
    guessed = SectionPlanCorrections(changes=[SectionChange(
        source_section_ids=["SRC-6"], target_key="ROLES", mapping_type=MappingType.SPLIT,
        unit_ids=["unit_id_responsibility"], reason="it names who is responsible")])
    result = await run_confirm(planner, proposal, FakeChain(guessed), planner.prompt(proposal))
    assert result.corrections.changes == []
    assert [(f.source_section_ids, f.note) for f in result.corrections.flags] == [
        (["SRC-6"], "may hold passages that belong to ROLES: it names who is responsible")]
    planner.apply(proposal, result.corrections)
    process = _by_target(planner.build_plan(proposal, "J", 1))["TGT-5"]
    assert process.status == MappingStatus.NEEDS_REVIEW and "belong to ROLES" in process.reason


def test_pdf_front_matter_before_chapter_one_is_omitted():
    """PDFs split the cover into unnumbered pseudo-sections ('GENERAL INFORMATION', 'Table of Content')."""
    d = _Doc()
    d.section("SRC-0", "0", "PREAMBLE", units=[("row", "Title | X", ["Field", "Value"], ["Title", "X"])], boilerplate=True)
    d.section("SRC-S01", None, "GENERAL INFORMATION", units=["Applicability Impacted Division(s) | Human Pharma"])
    d.section("SRC-S02", None, "Table of Content", parent="SRC-S01", units=["1 PURPOSE ........ 3", "2 SCOPE ........ 3"])
    d.section("SRC-1", "1", "PURPOSE", units=["This SOP describes the process."])
    d.section("SRC-2", "2", "PROCEDURE", units=[("procedure_step", "Record it."), ("procedure_step", "Approve it.")])
    d.section("SRC-2.1", "2.1", "Table of Contents", parent="SRC-2", units=["Step 1 ..... 4"])
    doc, tpl = d.build(), template()
    planner = SectionPlanner(doc, tpl)
    plan = planner.build_plan(planner.propose(), "J", 1)
    omitted = {s for m in plan.mappings if m.mapping_type == MappingType.OMIT for s in m.source_section_ids}
    assert {"SRC-0", "SRC-S01", "SRC-S02", "SRC-2.1"} <= omitted
    assert _by_target(plan)["TGT-5"].source_section_ids == ["SRC-2"]
    assert validate_section_plan(plan, doc, tpl).open_gate_issues == []

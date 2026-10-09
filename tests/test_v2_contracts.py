"""Tests for the v2 migration contracts (app/schemas/v2).

The example documents in docs/migration_v2/examples/ are the reference shapes
quoted in CONTRACTS.md; these tests keep them valid.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.v2 import (
    AnchorKind,
    CalloutAssignment,
    CalloutKind,
    CalloutStyle,
    Claim,
    ContentType,
    GwpExtractionReport,
    GwpRule,
    GwpRuleSet,
    JobStatus,
    MappingStatus,
    MappingType,
    MigrationAction,
    MigrationJob,
    AssembledDocument,
    NumberMap,
    Traceability,
    QualityReport,
    RenderReport,
    ReadinessStatus,
    RuleCategory,
    SectionDraft,
    SectionMapping,
    SectionPlan,
    SlotAnchor,
    SlotMapping,
    SlotPlan,
    SourceDocument,
    SourceUnit,
    TemplateModel,
    ArtifactKind,
    compute_content_hash,
    find_ref_tokens,
    make_ref_token,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "migration_v2" / "examples"

EXAMPLE_MODELS = {
    "source_document.json": SourceDocument,
    "template_model.json": TemplateModel,
    "gwp_rules.json": GwpRuleSet,
    "gwp_report.json": GwpExtractionReport,
    "section_plan.json": SectionPlan,
    "slot_plan.json": SlotPlan,
    "section_draft.json": SectionDraft,
    "quality_report.json": QualityReport,
    "migration_job.json": MigrationJob,
    "number_map.json": NumberMap,
    "render_report.json": RenderReport,
    "assembled_draft.json": AssembledDocument,
    "traceability.json": Traceability,
}


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


# ── Examples ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename, model", EXAMPLE_MODELS.items())
def test_example_validates_and_round_trips(filename, model):
    data = _load(filename)
    obj = model.model_validate(data)
    again = model.model_validate_json(obj.model_dump_json())
    assert again == obj


def test_every_example_file_is_covered():
    assert {p.name for p in EXAMPLES.glob("*.json")} == set(EXAMPLE_MODELS)


def test_unknown_fields_are_rejected():
    data = _load("section_plan.json")
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        SectionPlan.model_validate(data)


# ── Source ─────────────────────────────────────────────────────────────

def test_content_hash_ignores_whitespace_and_case_but_not_position():
    a = compute_content_hash("The  Process Owner\ncreates a record.", "4.2/1")
    b = compute_content_hash("the process owner creates a record.", "4.2/1")
    c = compute_content_hash("the process owner creates a record.", "4.2/2")
    assert a == b
    assert a != c


def test_table_row_requires_table_ref():
    with pytest.raises(ValidationError, match="table_ref"):
        SourceUnit(unit_id="U1", content_hash="h", section_id="S", seq=0, unit_type="table_row", text="a | b")


def test_duplicate_unit_ids_rejected():
    data = _load("source_document.json")
    units = data["sections"][0]["units"]
    units[1]["unit_id"] = units[0]["unit_id"]
    with pytest.raises(ValidationError, match="duplicate unit_id"):
        SourceDocument.model_validate(data)


# ── Template ───────────────────────────────────────────────────────────

def test_ready_template_cannot_have_unanchored_slots():
    data = _load("template_model.json")
    data["sections"][0]["slots"][0]["anchor"] = None
    with pytest.raises(ValidationError, match="unanchored"):
        TemplateModel.model_validate(data)


def test_paragraph_anchor_is_not_ready():
    data = _load("template_model.json")
    data["sections"][0]["slots"][0]["anchor"] = {"kind": "paragraph", "ref": "57"}
    with pytest.raises(ValidationError, match="unanchored"):
        TemplateModel.model_validate(data)
    data["readiness"] = ReadinessStatus.NEEDS_NORMALIZATION.value
    TemplateModel.model_validate(data)


def test_table_cell_anchor_needs_coordinates():
    with pytest.raises(ValidationError, match="table_cell"):
        SlotAnchor(kind=AnchorKind.TABLE_CELL, ref="T4R2C3", table_index=4)


def test_slot_section_mismatch_rejected():
    data = _load("template_model.json")
    data["sections"][0]["slots"][0]["section_id"] = "TGT-9"
    with pytest.raises(ValidationError, match="listed under"):
        TemplateModel.model_validate(data)


# ── Callouts ───────────────────────────────────────────────────────────

def test_hex_colour_is_normalized_and_validated():
    style = CalloutStyle(kind="attention", label="Attention", fill_hex="#f5cdb9")
    assert style.fill_hex == "F5CDB9"
    for bad in ("F5CDB", "GGGGGG", "auto"):
        with pytest.raises(ValidationError, match="hex"):
            CalloutStyle(kind="attention", label="Attention", fill_hex=bad)


def test_palette_kinds_are_unique():
    data = _load("template_model.json")
    data["callout_palette"][1]["kind"] = "introduction"
    with pytest.raises(ValidationError, match="duplicate callout kind"):
        TemplateModel.model_validate(data)


def test_slot_callout_kind_must_be_in_palette():
    data = _load("template_model.json")
    data["callout_palette"] = [p for p in data["callout_palette"] if p["kind"] != "introduction"]
    with pytest.raises(ValidationError, match="missing from the palette"):
        TemplateModel.model_validate(data)


def test_callout_profile_and_kind_go_together():
    data = _load("template_model.json")
    del data["sections"][0]["slots"][0]["callout_kind"]
    with pytest.raises(ValidationError, match="set together"):
        TemplateModel.model_validate(data)
    data = _load("template_model.json")
    data["sections"][0]["slots"][1]["callout_kind"] = "attention"
    with pytest.raises(ValidationError, match="set together"):
        TemplateModel.model_validate(data)


def test_ready_template_needs_prototype_for_used_callout_kinds():
    data = _load("template_model.json")
    data["callout_palette"][0]["prototype_table_index"] = None
    with pytest.raises(ValidationError, match="lacking a prototype"):
        TemplateModel.model_validate(data)
    data["readiness"] = ReadinessStatus.NEEDS_REVIEW.value
    TemplateModel.model_validate(data)


def test_unit_cannot_be_in_two_callouts():
    data = _load("slot_plan.json")
    section = data["sections"][0]
    section["callout_assignments"].append(
        {"unit_ids": ["SRC-4.2-U003"], "kind": "explanation", "origin": "llm", "reason": "x"}
    )
    with pytest.raises(ValidationError, match="more than one callout"):
        SlotPlan.model_validate(data)
    plan = SlotPlan.model_validate(_load("slot_plan.json")).sections[0]
    assert plan.callout_kind_for("SRC-4.2-U003") == CalloutKind.ATTENTION
    assert plan.callout_kind_for("SRC-4.2-U001") is None


def test_callout_assignment_needs_units_and_reason():
    with pytest.raises(ValidationError):
        CalloutAssignment(unit_ids=[], kind="attention", reason="warning")
    with pytest.raises(ValidationError):
        CalloutAssignment(unit_ids=["U1"], kind="attention", reason="")
    with pytest.raises(ValidationError):
        CalloutAssignment(unit_ids=["U1"], kind="danger", reason="not a palette kind")


# ── GWP ────────────────────────────────────────────────────────────────

def test_rule_id_must_match_category():
    with pytest.raises(ValidationError, match="must start with"):
        GwpRule(rule_id="STY-001", category=RuleCategory.PRESERVATION, text="x")


def test_rule_selection_by_category_content_type_and_status():
    rules = GwpRuleSet.model_validate(_load("gwp_rules.json"))

    drafting = rules.select([RuleCategory.STYLE, RuleCategory.PRESERVATION], [ContentType.ORDERED_PROCEDURE])
    assert [r.rule_id for r in drafting] == ["STY-002", "PRES-004"]

    timing = rules.select([RuleCategory.STYLE, RuleCategory.PRESERVATION], [ContentType.TIMING])
    assert [r.rule_id for r in timing] == ["PRES-004"]  # PRES-004 applies to every content type

    # FMT-001 is still a candidate
    assert rules.select([RuleCategory.FORMATTING]) == []
    assert [r.rule_id for r in rules.select([RuleCategory.FORMATTING], approved_only=False)] == ["FMT-001"]


# ── Plans ──────────────────────────────────────────────────────────────

def test_split_requires_unit_ids():
    with pytest.raises(ValidationError, match="split"):
        SectionMapping(target_section_id="TGT-6", source_section_ids=["SRC-7"], mapping_type=MappingType.SPLIT)


def test_omit_requires_justification():
    with pytest.raises(ValidationError, match="justification"):
        SectionMapping(source_section_ids=["SRC-0"], mapping_type=MappingType.OMIT, status=MappingStatus.NOT_APPLICABLE)


def test_mapped_slot_requires_units():
    with pytest.raises(ValidationError, match="no source_unit_ids"):
        SlotMapping(slot_id="TGT-3-STEPS", status=MappingStatus.MAPPED)


def test_not_found_slot_forces_review_and_no_action():
    slot = SlotMapping(slot_id="TGT-3-INPUTS", status=MappingStatus.SOURCE_CONTENT_NOT_FOUND)
    assert slot.requires_human_review is True
    assert slot.migration_action == MigrationAction.NONE

    with pytest.raises(ValidationError, match="lists source units"):
        SlotMapping(slot_id="X", status=MappingStatus.SOURCE_CONTENT_NOT_FOUND, source_unit_ids=["U1"])


# ── Drafts and cross-references ────────────────────────────────────────

def test_claim_without_sources_rejected_unless_gap_marker():
    with pytest.raises(ValidationError, match="no source_unit_ids"):
        Claim(claim_id="C1", text="Invented content.")
    Claim(claim_id="GAP", text="Source content not found.", is_gap_marker=True)


def test_ref_tokens():
    token = make_ref_token("SRC-5.1")
    assert token == "{{ref:SRC-5.1}}"
    assert find_ref_tokens(f"See {token} and {{{{ref:SRC-4.2-U002}}}}.") == ["SRC-5.1", "SRC-4.2-U002"]

    draft = SectionDraft.model_validate(_load("section_draft.json"))
    refs = [r for claim in draft.iter_claims() for r in claim.ref_targets]
    assert refs == ["SRC-5.1"]


def test_number_map_lookup():
    numbers = NumberMap.model_validate(_load("number_map.json"))
    assert numbers.lookup("SRC-5.1").target_number == "4.1"
    assert numbers.lookup("SRC-9") is None
    assert numbers.lookup("SRC-4.2-U002").item_number == "2" and not numbers.lookup("SRC-5.3").exact


def test_assembled_document_ref_lookup():
    assembled = AssembledDocument.model_validate(_load("assembled_draft.json"))
    ref = assembled.ref("C-TGT-3-003", "SRC-5.1")
    assert ref.text[ref.number_at:].startswith(ref.number) and ref.status.value == "resolved"
    assert assembled.ref("C-TGT-3-003", "SRC-9") is None


# ── Quality and job ────────────────────────────────────────────────────

def test_gates_pass_only_when_gate_issues_resolved():
    report = QualityReport.model_validate(_load("quality_report.json"))
    assert not report.gates_passed
    assert [i.issue_id for i in report.open_gate_issues] == ["ISS-001"]

    report.issues[0].resolved = True
    assert report.gates_passed


def test_job_latest_artifact_and_terminal_status():
    job = MigrationJob.model_validate(_load("migration_job.json"))
    assert job.latest_artifact(ArtifactKind.SECTION_PLAN).version == 2
    assert job.latest_artifact(ArtifactKind.DOCX) is None
    assert not job.is_terminal

    job.status = JobStatus.COMPLETED
    assert job.is_terminal


def test_render_report_ok_and_slot_lookup():
    report = RenderReport.model_validate(_load("render_report.json"))
    assert report.ok and report.slot("TGT-2-GEOGRAPHY").outcome.value == "gap_marker"
    assert report.slot("TGT-X") is None
    assert not report.model_copy(update={"problems": ["a claim is missing"]}).ok

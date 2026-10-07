"""Phase 3 on the real sample template (confidential, gitignored): slots, normalization, readiness.

Skips when the template is absent. The reviewed region decisions live in
``documents/template_config/Template_Main_GP_Docs.json``.
"""

import json
from pathlib import Path

import pytest
from docx import Document

from app.schemas.v2 import AnchorKind, CalloutKind, FormattingProfile, ReadinessStatus
from app.services.migration_v2.template.overrides import load_config
from app.services.migration_v2.template.service import build_template_model, normalize_and_save
from app.services.parser.ooxml import element_text, iter_body_blocks

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = next(
    (p for p in (ROOT / "documents" / "Templates" / "Template Main GP Docs.docx",
                 ROOT / "data" / "samples" / "template" / "Template Main GP Docs.docx") if p.exists()),
    None,
)
CONFIG = ROOT / "documents" / "template_config" / "Template_Main_GP_Docs.json"
GOLDEN_DIR = ROOT / "documents" / "golden"

pytestmark = pytest.mark.skipif(TEMPLATE is None, reason="sample template not available")


@pytest.fixture(scope="module")
def undecided():
    return build_template_model(TEMPLATE, "Template_Main_GP_Docs", 1)


@pytest.fixture(scope="module")
def normalized(tmp_path_factory):
    if not CONFIG.exists():
        pytest.skip("template config not available")
    out = tmp_path_factory.mktemp("template_v2")
    return normalize_and_save(TEMPLATE, "Template_Main_GP_Docs", 1, out, load_config(CONFIG))


def test_sections_follow_the_template_chapters(undecided):
    model = undecided.model
    assert [(s.number, s.key) for s in model.sections] == [
        ("1", "PURPOSE"), ("2", "APPLICABILITY"), ("3", "DEFINITIONS"), ("4", "IMPLEMENTATION"),
        ("5", "ROLES"), ("6", "PROCESS"), ("7", "ASSOCIATED_DOCUMENTS"), ("8", "REFERENCES"),
        ("9", "DISTRIBUTION_OF_CONTROLLED_PRINTS"), ("10", "DOCUMENT_HISTORY"),
    ]
    optional = [s.key for s in model.sections if not s.required]
    assert optional == ["DISTRIBUTION_OF_CONTROLLED_PRINTS"]
    assert model.sections[8].optional_marker


def test_every_golden_slot_ref_exists(undecided):
    refs = set()
    for path in GOLDEN_DIR.glob("*.json") if GOLDEN_DIR.exists() else []:
        golden = json.loads(path.read_text(encoding="utf-8"))
        refs |= {e["target_slot_ref"] for e in golden.get("slot_expectations", []) if e.get("target_slot_ref")}
        refs |= {e["slot_ref"] for e in golden.get("expected_empty_slots", [])}
    refs |= {"PURPOSE.what", "PURPOSE.intention", "APPLICABILITY.roles", "APPLICABILITY.units",
             "APPLICABILITY.geography", "APPLICABILITY.processes", "APPLICABILITY.not_covered"}
    missing = sorted(r for r in refs if undecided.model.slot_by_ref(r) is None)
    assert missing == []


def test_icon_rows_and_not_covered(undecided):
    model = undecided.model
    icon_rows = ["PURPOSE.what", "PURPOSE.intention", "APPLICABILITY.roles", "APPLICABILITY.units",
                 "APPLICABILITY.geography", "APPLICABILITY.processes"]
    for ref in icon_rows:
        slot = model.slot_by_ref(ref)
        assert slot.icon is not None and slot.anchor.kind == AnchorKind.TABLE_CELL, ref
        assert slot.anchor.column_index == 1
    assert len({model.slot_by_ref(r).icon.icon_key for r in icon_rows}) == 6
    assert model.slot_by_ref("APPLICABILITY.processes").required is False  # "If applicable, ..."
    not_covered = model.slot_by_ref("APPLICABILITY.not_covered")
    assert not_covered.icon is None and not_covered.anchor.kind == AnchorKind.PARAGRAPH


def test_callout_prototypes_are_fixed_callout_slots(undecided):
    model = undecided.model
    callouts = [s for s in model.iter_slots() if s.formatting_profile == FormattingProfile.CALLOUT]
    assert [(s.key, s.callout_kind) for s in callouts] == [
        ("callout_introduction", CalloutKind.INTRODUCTION),
        ("callout_explanation", CalloutKind.EXPLANATION),
        ("callout_attention", CalloutKind.ATTENTION),
        ("callout_key_takeaway", CalloutKind.KEY_TAKEAWAY),
    ]
    assert all(s.section_id == "TGT-6" and not s.required for s in callouts)
    prototypes = {p.kind: p.prototype_table_index for p in model.callout_palette}
    assert [s.anchor.table_index for s in callouts] == [prototypes[s.callout_kind] for s in callouts]
    process = model.slot_by_ref("PROCESS.content")
    assert process.required and "infographic at the beginning" not in process.instruction


def test_table_slots(undecided):
    model = undecided.model
    tables = {f"{s.section_id}:{s.key}" for s in model.iter_slots() if s.formatting_profile == FormattingProfile.TABLE}
    assert tables == {"TGT-3:terms", "TGT-3:abbreviations", "TGT-5:roles", "TGT-5:raci",
                      "TGT-7:documents", "TGT-8:documents", "TGT-10:entries"}
    assert model.slot_by_ref("ROLES.raci").anchor.row_index == 2  # below the "[Role n]" header row


def test_undecided_template_needs_review(undecided):
    assert undecided.report.status == ReadinessStatus.NEEDS_REVIEW
    assert sorted(a.region_id for a in undecided.detection.unresolved) == ["p:26", "p:32", "t:7"]


def test_normalized_template_is_ready_and_round_trips(undecided, normalized):
    assert normalized.normalization.skipped == []
    assert normalized.build.report.status == ReadinessStatus.READY, normalized.build.report.blocking
    model = normalized.build.model
    assert all(s.anchor.kind == AnchorKind.CONTENT_CONTROL for s in model.iter_slots())
    assert model.slot_by_ref("APPLICABILITY.geography").anchor.ref == "CC_APPLICABILITY_GEOGRAPHY"
    summary = lambda m: [(s.slot_id, s.key, s.content_type, s.instruction, s.callout_kind) for s in m.iter_slots()]
    decided = build_template_model(TEMPLATE, "Template_Main_GP_Docs", 1, load_config(CONFIG))
    assert summary(model) == summary(decided.model)
    assert [s.slot_id for s in model.iter_slots()] == [s.slot_id for s in undecided.model.iter_slots()]
    # The doc-type lines and the competence table depend on the SOP: conditional, each in its own control.
    conditional = {r.region_id: (s.key, r.kind.value, r.anchor.ref) for s in model.sections for r in s.conditional_regions}
    assert conditional == {
        "p:26": ("PURPOSE", "inline_choice", "COND_PURPOSE_1"),
        "p:32": ("APPLICABILITY", "inline_choice", "COND_APPLICABILITY_1"),
        "t:7": ("ROLES", "block", "COND_ROLES_1"),
    }
    assert "Competence | Description" not in model.slot_by_ref("ROLES.roles").instruction
    # Table A, B, or both.
    roles = next(s for s in model.sections if s.key == "ROLES")
    assert roles.one_of == [["roles", "raci"]]
    assert not model.slot_by_ref("ROLES.roles").required and not model.slot_by_ref("ROLES.raci").required

    text = lambda p: [element_text(b) for b in iter_body_blocks(Document(str(p)).element.body)]
    assert text(normalized.normalized_path) == text(TEMPLATE)

"""Phase 4 checks on the real GWP guide (BI-VQD-24416-G, PDF).

The guide is confidential and gitignored (``documents/GWP/``), so every test
skips when it is absent. ``*_rules.json`` and ``*_report.json`` next to it are
the curated candidate rules and their coverage report.
"""

import json
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.schemas.v2 import GwpExtractionReport, GwpRuleSet, RuleCategory, RuleCheck
from app.services.migration_v2.gwp import service
from app.services.migration_v2.gwp.extractor import build_batches
from app.services.migration_v2.gwp.ingest import parse_guide, rule_input_sections

ROOT = Path(__file__).resolve().parent.parent
GUIDE = next(
    (p for p in (ROOT / "documents" / "GWP" / "BI-VQD-24416-G.pdf", ROOT / "data" / "samples" / "gwp" / "BI-VQD-24416-G.pdf") if p.exists()),
    None,
)
pytestmark = pytest.mark.skipif(GUIDE is None, reason="sample GWP guide not available")
GUIDE_ID = "BI-VQD-24416-G"


@pytest.fixture(scope="module")
def guide(tmp_path_factory):
    root = tmp_path_factory.mktemp("gwp")
    settings = Settings(project_root=root)
    settings.resolve_paths(root)
    settings.ensure_directories()
    return parse_guide(GUIDE, settings, GUIDE_ID)


def _text(doc) -> str:
    return " ".join(u.text for u in doc.iter_units())


def test_guide_sections(guide):
    numbered = [s.number for s in guide.sections if s.number]
    for number in ("1", "2", "3", "4", "5", "6", "6.2", "6.3", "6.4", "6.5", "6.5.1", "6.5.2", "6.5.3",
                   "7", "7.1", "7.2", "7.3", "7.4", "7.5", "8", "9", "10"):
        assert number in numbered


def test_text_near_page_edges_is_kept(guide):
    """Body text in the header/footer zones used to be dropped (PDFLayoutAnalyzer read ``el.text``)."""
    text = _text(guide)
    assert 'Use personal pronouns like "I" and "you" where appropriate.' in text  # last line above a footer
    assert "it implies that the role does not have any responsibility in the process" in text  # first line below a header


def test_running_headers_and_footers_are_stripped(guide):
    texts = [u.text for u in guide.iter_units() if not u.is_boilerplate]
    assert "Good Writing Practice for Governance and Procedure Documents" not in texts
    joined = " ".join(texts)
    assert "Retrieved by" not in joined and "Property of Boehringer" not in joined


def test_rule_input_excludes_toc_references_and_history(guide):
    headings = [s.heading.lower() for s, _ in rule_input_sections(guide)]
    assert not any(h in headings for h in ("table of content", "references", "associated documents", "document history"))
    assert sum(len(units) for _, units in rule_input_sections(guide)) > 100
    assert len(build_batches(guide)) == 2  # about 25k chars of guide text


CURATED = GUIDE.parent / f"{GUIDE_ID}_rules.json" if GUIDE else None
REPORT = GUIDE.parent / f"{GUIDE_ID}_report.json" if GUIDE else None


@pytest.mark.skipif(CURATED is None or not CURATED.exists(), reason="curated GWP rules not available")
def test_curated_rules_cite_the_guide_and_cover_it(guide):
    rule_set = GwpRuleSet.model_validate(json.loads(CURATED.read_text(encoding="utf-8")))
    assert service.validate_rule_set(rule_set, GUIDE_ID, rule_set.version, guide) == []

    report = GwpExtractionReport.model_validate(json.loads(REPORT.read_text(encoding="utf-8")))
    input_ids = {u.unit_id for _, units in rule_input_sections(guide) for u in units}
    assert set(report.input_unit_ids) == input_ids, "unit IDs drifted; regenerate the curated rules"
    cited = {i for r in rule_set.rules for i in r.source_unit_ids} | {i for s in report.skipped for i in s.source_unit_ids}
    assert input_ids <= cited

    by_id = {r.rule_id: r for r in rule_set.rules}
    assert by_id["FMT-001"].params["colors"] == {
        "introduction": "D2F2F7", "explanation": "00E47C", "attention": "F5CDB9", "key_takeaway": "FBF9AA",
    }
    assert by_id["STY-008"].check == RuleCheck.DETERMINISTIC and by_id["STY-008"].params["terms"] == ["please"]

    approved, _ = service.approve_rules(rule_set)
    assert {r.category for r in service.select_rules(approved, [RuleCategory.STRUCTURAL])} == {RuleCategory.STRUCTURAL}

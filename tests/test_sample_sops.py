"""Regression tests on the real sample SOPs and their reviewed golden expectations.

The SOPs are confidential and gitignored (``documents/`` or ``data/samples/``),
so every test skips when they are absent. The golden format is described in
``tests/fixtures/migration_v2/README.md``.
"""

import json
import re
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn

from app.config.settings import Settings
from app.schemas.document import ElementType
from app.schemas.v2 import AssetKind, RefResolution, UnitType
from app.services.export.migration_exporter import MigrationExporter
from app.services.export.source_unit_exporter import SourceUnitExporter
from app.services.extraction.captions import CaptionExtractor
from app.services.extraction.cross_page_stitcher import CrossPageTableStitcher
from app.services.extraction.icons import IconExtractor
from app.services.extraction.tables import TableExtractor
from app.services.hierarchy.ast_builder import ASTBuilder
from app.services.parser.docx_parser import DocxParser
from app.services.parser.ooxml import element_text, is_toc_sdt

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ROOTS = [ROOT / "documents", ROOT / "data" / "samples"]


def _find(sub: str, name: str):
    for root in SAMPLE_ROOTS:
        for folder in (root / sub, root / sub.lower()):
            if (folder / name).exists():
                return folder / name
    return None


def _goldens() -> list[tuple[Path, dict]]:
    found = []
    for root in SAMPLE_ROOTS:
        for path in sorted((root / "golden").glob("*.json")) if (root / "golden").exists() else []:
            golden = json.loads(path.read_text(encoding="utf-8"))
            sop = _find("SOPs", golden["sop_file"]) or _find("sop", golden["sop_file"])
            if sop is not None:
                found.append((sop, golden))
    return found


GOLDENS = _goldens()
pytestmark = pytest.mark.skipif(not GOLDENS, reason="sample SOPs and goldens not available")

# Cross-reference targets named in the goldens, as Word heading numbers.
EXPECTED_HEADING_NUMBERS = {
    "028-BIS-00493.docx": {"6.1.2.1"},
    "BI-VQD-10505-S.docx": {"6.2"},
}
RPAS = "028-BIS-00535_1.0_Validation and operational use of Robotic Process Automation Systems (RPAS).docx"
EXPECTED_HEADING_NUMBERS[RPAS] = {"5.2", "6.4", "6.8", "6.12.1"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _strip_number(title: str) -> str:
    return re.sub(r"^\d+(?:\.\d+)*\s+", "", title).strip()


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    s = Settings(project_root=root)
    s.resolve_paths(root)
    s.ensure_directories()
    return s


@pytest.fixture(scope="module")
def runs(settings):
    """Run the full v3.1 DOCX pipeline once per sample SOP."""
    out = {}
    for sop, golden in GOLDENS:
        doc_id = re.sub(r"\W", "_", sop.stem)[:30]
        raw = DocxParser(settings=settings).parse(str(sop), document_id=doc_id)
        parsed = raw
        raw = TableExtractor(settings=settings).extract(raw)
        raw = CrossPageTableStitcher(settings=settings).extract(raw)
        raw = IconExtractor(settings=settings).extract(raw, document_id=doc_id)
        raw = CaptionExtractor(settings=settings).extract(raw)
        ast = ASTBuilder(settings=settings).build(raw)
        export = MigrationExporter.export(document_id=doc_id, ast=ast)
        units = SourceUnitExporter.export(doc_id, ast, source_file=str(sop))
        out[sop.name] = (sop, golden, parsed, export, units)
    return out


def _cases():
    return [pytest.param(sop.name, id=sop.stem[:20]) for sop, _ in GOLDENS]


def _export_text(export) -> str:
    return _norm(json.dumps(export.to_clean_dict(), ensure_ascii=False))


@pytest.mark.parametrize("name", _cases())
def test_sections_match_golden(runs, name):
    _, golden, _, export, _ = runs[name]
    expected = [m["source_heading"] for m in golden["section_mapping"]
                if m["source_heading"] and " > " not in m["source_heading"]]
    titles = [s.title for s in export.sections]
    assert titles[0] == "0 PREAMBLE"  # the cover sheet only
    assert [_norm(_strip_number(t)) for t in titles[1:]] == [_norm(h) for h in expected]
    assert [s.section_number for s in export.sections[1:]] == [str(i) for i in range(1, len(expected) + 1)]


@pytest.mark.parametrize("name", _cases())
def test_heading_numbers_match_the_documents_own_toc(runs, name):
    sop, _, parsed, _, _ = runs[name]
    computed = {
        _norm(el.content): el.metadata.get("numbering")
        for page in parsed.pages for el in page.elements
        if el.element_type == ElementType.HEADING
    }
    toc_entries = []
    body = Document(str(sop)).element.body
    for sdt in body.iter(qn("w:sdt")):
        if not is_toc_sdt(sdt):
            continue
        for p in sdt.iter(qn("w:p")):
            parts = [x.strip() for x in element_text(p).split("\t") if x.strip()]
            if len(parts) >= 2 and re.fullmatch(r"\d+(?:\.\d+)*", parts[0]):
                toc_entries.append((parts[0], _norm(parts[1])))
    assert toc_entries, "no numbered TOC entries found"
    checked = [(num, title) for num, title in toc_entries if title in computed]
    assert len(checked) >= len(toc_entries) * 0.8
    assert [(num, title) for num, title in checked if computed[title] != num] == []


@pytest.mark.parametrize("name", _cases())
def test_cross_reference_targets_exist(runs, name):
    _, _, parsed, _, _ = runs[name]
    numbers = {
        el.metadata.get("numbering")
        for page in parsed.pages for el in page.elements
        if el.element_type == ElementType.HEADING
    }
    assert EXPECTED_HEADING_NUMBERS[name] <= numbers


@pytest.mark.parametrize("name", _cases())
def test_must_preserve_facts_survive_extraction(runs, name):
    _, golden, _, export, _ = runs[name]
    text = _export_text(export)
    missing = [fact for fact in golden["must_preserve"] if _norm(fact) not in text]
    assert missing == []


def test_bi_vqd_metadata_reads_content_controls(runs):
    if "BI-VQD-10505-S.docx" not in runs:
        pytest.skip("BI-VQD-10505-S not available")
    meta = runs["BI-VQD-10505-S.docx"][3].metadata
    assert meta.document_version == "5.0"
    assert meta.document_number == "BI-VQD-10505"
    assert meta.document_type == "Governance and Procedure > Standard Operating Procedure (SOP)"


def test_rpas_prohibited_uses_are_one_list(runs):
    if RPAS not in runs:
        pytest.skip("RPAS SOP not available")
    export = runs[RPAS][3]
    lists = [e for s in export.sections for e in s.elements if e.element_type == "list"]
    prohibited = [l for l in lists if any("Placing an electronic signature" in i for i in l.items)]
    assert len(prohibited) == 1
    assert len(prohibited[0].items) == 7


# ── v2 source units (Phase 2b) ─────────────────────────────────────────

def _ancestors(doc, section_id):
    by_id = {s.section_id: s for s in doc.sections}
    chain, current = [], by_id.get(section_id)
    while current is not None:
        chain.append(current)
        current = by_id.get(current.parent_id)
    return chain


def _section_of(doc, unit_id):
    return next(s for s in doc.sections if any(u.unit_id == unit_id for u in s.units))


def _units_text(doc) -> str:
    return _norm(" ".join(u.text for u in doc.iter_units()))


@pytest.mark.parametrize("name", _cases())
def test_units_lose_no_text(runs, name):
    from app.services.migration_v2.inspection.completeness import check_completeness

    sop, _, _, _, units = runs[name]
    result = check_completeness(sop, units)
    assert result.missing_words == [], result.missing_context[:3]
    assert result.recall == 1.0 and result.source_words > 1000


@pytest.mark.parametrize("name", _cases())
def test_slot_evidence_exists_as_units(runs, name):
    _, golden, _, _, units = runs[name]
    text = _units_text(units)
    missing = [e["source_text_contains"] for e in golden["slot_expectations"] if _norm(e["source_text_contains"]) not in text]
    assert missing == []


@pytest.mark.parametrize("name", _cases())
def test_icon_rows_are_metadata_on_their_text(runs, name):
    _, golden, _, _, units = runs[name]
    for row in golden.get("source_icon_rows", []):
        section = next(s for s in units.sections if _norm(s.heading) == _norm(row["section"]))
        with_icon = [u for u in section.units if any(a.kind == AssetKind.ICON for a in u.assets)]
        assert len(with_icon) == row["rows"], (row["section"], [u.text[:40] for u in with_icon])
    if not golden.get("source_icon_rows"):
        assert not any(a.kind == AssetKind.ICON for u in units.iter_units() for a in u.assets)


@pytest.mark.parametrize("name", _cases())
def test_figures_stay_figures_with_captions(runs, name):
    _, golden, _, _, units = runs[name]
    figures = [u for u in units.iter_units() if u.unit_type == UnitType.FIGURE]
    assert len(figures) == len(golden.get("figures", []))
    captions = [u for u in units.iter_units() if u.unit_type == UnitType.CAPTION]
    for expected in golden.get("figures", []):
        caption = next(c for c in captions if _norm(expected["caption_contains"]) in _norm(c.text))
        (relation,) = caption.relations
        figure = next(f for f in figures if f.unit_id == relation.target_unit_id)
        headings = [_norm(s.heading) for s in _ancestors(units, figure.section_id)]
        assert _norm(expected["section"].split(" > ")[-1]) in headings
        assert [a.kind for a in figure.assets] == [AssetKind.FIGURE]


@pytest.mark.parametrize("name", _cases())
def test_golden_cross_references_resolve(runs, name):
    _, golden, _, _, units = runs[name]
    for expected in golden["cross_references"]:
        source = _norm(expected["source_text"]).lower()
        ref = next(r for r in units.cross_references if _norm(r.raw_text).lower() in source)
        assert ref.resolution == RefResolution.RESOLVED, expected
        target_heading = expected["expected_target_heading"]
        if "#" in target_heading:
            section_name, item = re.match(r"(.+?)\s*#(\d+)", target_heading).groups()
            row = next(u for u in units.iter_units() if u.unit_id == ref.target)
            assert row.table_ref.cells[0].text.strip() == item
            assert _norm(_section_of(units, row.unit_id).heading).lower() in (_norm(section_name).lower(), "associated documents", "references")
        else:
            wanted = _norm(re.sub(r"\(.*?\)", "", target_heading.split(" > ")[-1])).lower()
            headings = [_norm(s.heading).lower() for s in _ancestors(units, ref.target)]
            assert wanted in headings, (expected, headings)


@pytest.mark.parametrize("name", _cases())
def test_cover_sheet_and_history_are_boilerplate(runs, name):
    _, _, _, _, units = runs[name]
    preamble = units.sections[0]
    assert preamble.heading == "PREAMBLE" and preamble.units
    assert all(u.is_boilerplate for u in preamble.units)
    history = next(s for s in units.sections if "history" in s.heading.lower())
    assert all(u.is_boilerplate for u in history.units)
    body = [u for s in units.sections[1:] if "history" not in s.heading.lower() for u in s.units]
    assert body and not any(u.is_boilerplate for u in body)

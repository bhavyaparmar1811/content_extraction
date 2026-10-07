"""Parse a GWP guide (.docx or .pdf) into ordered, citable source units.

Runs the same extraction pipeline as SOP uploads (parse, tables, icons,
captions, AST, ``SourceUnitExporter``), so rule citations point at the same
kind of unit IDs as everywhere else in v2.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config.settings import Settings
from app.schemas.v2 import SourceDocument, SourceSection, SourceUnit
from app.services.export.source_unit_exporter import SourceUnitExporter
from app.services.extraction.captions import CaptionExtractor
from app.services.extraction.cross_page_stitcher import CrossPageTableStitcher
from app.services.extraction.icons import IconExtractor
from app.services.extraction.tables import TableExtractor
from app.services.hierarchy.ast_builder import ASTBuilder
from app.services.parser.parser_factory import ParserFactory

# Sections of the guide that hold no writing rules.
_NO_RULE_HEADING_RE = re.compile(
    r"^(?:table\s+of\s+contents?|contents|general\s+information|references?|associated\s+documents?"
    r"|document\s+history|revision\s+history|document\s+approvals?)$",
    re.I,
)


def parse_guide(path: Path, settings: Settings, guide_id: str) -> SourceDocument:
    """Run the extraction pipeline on a guide file and export its source units."""
    path = Path(path)
    raw = ParserFactory(settings=settings).get_parser(str(path)).parse(str(path), document_id=guide_id)
    raw = TableExtractor(settings=settings).extract(raw)
    raw = CrossPageTableStitcher(settings=settings).extract(raw)
    raw = IconExtractor(settings=settings).extract(raw, document_id=guide_id)
    raw = CaptionExtractor(settings=settings).extract(raw)
    ast = ASTBuilder(settings=settings).build(raw)
    return SourceUnitExporter.export(
        document_id=guide_id,
        ast=ast,
        source_file=path.name,
        file_type=path.suffix.lstrip(".").lower() or None,
    )


# The pipeline is not GWP-specific: SOPs and guides go through the same extraction.
parse_document = parse_guide


def _heading_text(section: SourceSection) -> str:
    return re.sub(r"\s+", " ", section.heading).strip()


def is_rule_bearing(section: SourceSection) -> bool:
    """False for the cover page, table of contents, references and history."""
    if section.number == "0":
        return False
    return not _NO_RULE_HEADING_RE.match(_heading_text(section))


def rule_input_sections(doc: SourceDocument) -> list[tuple[SourceSection, list[SourceUnit]]]:
    """Sections the extractor reads, with their non-boilerplate units, in document order."""
    selected = []
    for section in doc.sections:
        if not is_rule_bearing(section):
            continue
        units = [u for u in section.units if not u.is_boilerplate and u.text.strip()]
        if units:
            selected.append((section, units))
    return selected


def render_section(section: SourceSection, units: list[SourceUnit]) -> str:
    """The prompt form of one section: heading line, then one line per unit."""
    heading = " ".join(part for part in (section.number, _heading_text(section)) if part)
    lines = [f"## {heading}"]
    for unit in units:
        lines.append(f"[{unit.unit_id}] ({unit.unit_type.value}) {unit.text}")
    return "\n".join(lines)

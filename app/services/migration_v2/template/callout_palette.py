"""Read the template's colour-coded callout boxes into a ``CalloutStyle`` palette.

A callout box is a table row whose cells all share one fill: either an
image-only icon cell followed by a text cell, or a single text cell. The
template's legend table ("Infographics | Description") names each colour;
single-row boxes elsewhere in the body are prototypes that rendering clones.
Prototypes are matched to the legend by fill, so a box's kind never depends on
its instruction text.

Shaded rows that sit beside unshaded cells (for example the RACI matrix) are
not callouts.

Highlighted runs (``w:highlight``) are reported too. A highlighted "(optional)"
in a heading marks that section as optional.

``table_index`` counts top-level body tables in document order, including
tables wrapped in block-level content controls (``iter_body_blocks``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping, Optional

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn

from app.schemas.v2 import CalloutKind, CalloutLayout, CalloutOrigin, CalloutStyle
from app.services.parser.docx_parser import DocxParser
from app.services.parser.ooxml import iter_body_blocks
from app.services.parser.template_parser import TemplateDocxParser

_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

# Checked in order; the first match wins.
_KIND_KEYWORDS: list[tuple[CalloutKind, re.Pattern]] = [
    (CalloutKind.KEY_TAKEAWAY, re.compile(r"key[\s-]*take[\s-]*(a\s*)?way", re.I)),
    (CalloutKind.INTRODUCTION, re.compile(r"introduction|executive\s+summary", re.I)),
    (CalloutKind.ATTENTION, re.compile(r"attention|warning|caution", re.I)),
    (CalloutKind.EXPLANATION, re.compile(r"explanation", re.I)),
]

_LEGEND_HEADER = re.compile(r"infographic", re.I)


@dataclass
class CalloutPrototype:
    """A single-row callout box in the template body: a fixed callout slot."""

    table_index: int
    kind: CalloutKind
    fill_hex: str
    instruction: str
    section_heading: Optional[str]


@dataclass
class HighlightedText:
    paragraph_text: str
    run_text: str
    color: str
    heading: Optional[str]


@dataclass
class PaletteResult:
    palette: list[CalloutStyle] = field(default_factory=list)
    prototypes: list[CalloutPrototype] = field(default_factory=list)
    legend_table_index: Optional[int] = None
    optional_section_headings: list[str] = field(default_factory=list)
    highlights: list[HighlightedText] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def style_for(self, kind: CalloutKind) -> Optional[CalloutStyle]:
        return next((s for s in self.palette if s.kind == kind), None)


@dataclass
class _Box:
    """One callout row as found in the document."""

    table_index: int
    fill_hex: str
    layout: CalloutLayout
    text: str
    icon_key: Optional[str]
    text_color_hex: Optional[str]
    column_widths_pt: list[float]
    is_instruction: bool


def kind_for_label(text: Optional[str]) -> Optional[CalloutKind]:
    """Map a legend label such as "Key take way - ..." to a callout kind."""
    if not text:
        return None
    for kind, pattern in _KIND_KEYWORDS:
        if pattern.search(text):
            return kind
    return None


def extract_callout_palette(
    docx_path: str | Path,
    fill_overrides: Optional[Mapping[str, CalloutKind | str]] = None,
) -> PaletteResult:
    """Build the callout palette for a template.

    ``fill_overrides`` maps a fill hex to a kind and wins over the label
    keywords; it comes from ``data/template_config/{template_uid}.json``.
    """
    return palette_from_document(Document(str(docx_path)), fill_overrides)


def palette_from_document(doc, fill_overrides: Optional[Mapping[str, CalloutKind | str]] = None) -> PaletteResult:
    """``extract_callout_palette`` on an already opened ``docx.Document``."""
    overrides = {k.strip().lstrip("#").upper(): CalloutKind(v) for k, v in (fill_overrides or {}).items()}
    result = PaletteResult()

    legend: list[_Box] = []
    prototype_boxes: list[tuple[_Box, Optional[str]]] = []
    for table_index, tbl, heading in _iter_tables(doc):
        rows = tbl.findall(qn("w:tr"))
        boxes = [_row_box(doc, tbl, tr, table_index) for tr in rows]
        header_text = block_text(rows[0]) if rows else ""
        if _LEGEND_HEADER.search(header_text) or (len(rows) > 1 and all(boxes)):
            found = [b for b in boxes if b is not None]
            if found and result.legend_table_index is None:
                result.legend_table_index = table_index
                legend = found
                continue
        if len(rows) == 1 and boxes[0] is not None:
            prototype_boxes.append((boxes[0], heading))

    by_fill: dict[str, CalloutStyle] = {}
    for box in legend:
        kind = overrides.get(box.fill_hex) or kind_for_label(box.text)
        if kind is None:
            result.issues.append(f"legend colour {box.fill_hex} ('{box.text[:60]}') matches no callout kind")
            continue
        if any(s.kind == kind for s in by_fill.values()):
            result.issues.append(f"legend colour {box.fill_hex} repeats callout kind {kind.value}; ignored")
            continue
        by_fill[box.fill_hex] = CalloutStyle(
            kind=kind,
            label=_legend_label(box.text),
            fill_hex=box.fill_hex,
            icon_key=box.icon_key,
            text_color_hex=box.text_color_hex,
            layout=box.layout,
            column_widths_pt=box.column_widths_pt,
            origin=CalloutOrigin.LEGEND,
        )

    for box, heading in prototype_boxes:
        style = by_fill.get(box.fill_hex)
        if style is None:
            kind = overrides.get(box.fill_hex) or kind_for_label(box.text)
            if kind is None or any(s.kind == kind for s in by_fill.values()):
                result.issues.append(
                    f"callout box in table {box.table_index} has colour {box.fill_hex} with no palette entry"
                )
                continue
            style = CalloutStyle(
                kind=kind,
                label=_legend_label(box.text),
                fill_hex=box.fill_hex,
                icon_key=box.icon_key,
                layout=box.layout,
                column_widths_pt=box.column_widths_pt,
                origin=CalloutOrigin.PROTOTYPE,
            )
            by_fill[box.fill_hex] = style
        if style.prototype_table_index is None:
            style.prototype_table_index = box.table_index
            style.layout = box.layout
            style.column_widths_pt = box.column_widths_pt
            style.icon_key = style.icon_key or box.icon_key
        result.prototypes.append(
            CalloutPrototype(
                table_index=box.table_index,
                kind=style.kind,
                fill_hex=box.fill_hex,
                instruction=box.text,
                section_heading=heading,
            )
        )

    order = list(CalloutKind)
    result.palette = sorted(by_fill.values(), key=lambda s: order.index(s.kind))
    for style in result.palette:
        if style.icon_key is None and style.layout == CalloutLayout.ICON_TEXT_TWO_CELL:
            result.issues.append(f"callout kind {style.kind.value} has no icon")
        if style.prototype_table_index is None:
            result.issues.append(f"callout kind {style.kind.value} has no prototype box to clone")

    result.highlights, result.optional_section_headings = _highlights(doc)
    for h in result.highlights:
        if h.heading is None or "optional" not in h.run_text.lower():
            result.issues.append(f"highlighted template text, review: '{h.run_text[:60]}' in '{h.paragraph_text[:60]}'")
    return result


# ── Document walking ───────────────────────────────────────────────────

def _iter_tables(doc) -> Iterator[tuple[int, object, Optional[str]]]:
    heading: Optional[str] = None
    index = 0
    for el in iter_body_blocks(doc.element.body):
        if el.tag == qn("w:p"):
            if is_heading(doc, el):
                heading = block_text(el) or heading
            continue
        yield index, el, heading
        index += 1


def style_name(doc, p) -> str:
    style_id = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    if style_id is None:
        return ""
    try:
        return doc.styles.get_by_id(style_id.get(qn("w:val")), WD_STYLE_TYPE.PARAGRAPH).name or ""
    except Exception:
        return style_id.get(qn("w:val")) or ""


def is_heading(doc, p) -> bool:
    return style_name(doc, p).lower().startswith(("heading", "title"))


def block_text(el) -> str:
    paragraphs = el.iter(qn("w:p")) if el.tag != qn("w:p") else [el]
    parts = ["".join(t.text or "" for t in p.iter(qn("w:t"))) for p in paragraphs]
    return " ".join(" ".join(parts).split())


# ── Callout rows ───────────────────────────────────────────────────────

def _row_box(doc, tbl, tr, table_index: int) -> Optional[_Box]:
    cells = tr.findall(qn("w:tc"))
    if not cells:
        return None
    fills = {DocxParser._shading_fill(tc.find(qn("w:tcPr"))) for tc in cells}
    if len(fills) != 1 or None in fills:
        return None
    fill = fills.pop()

    if len(cells) == 1:
        layout, text_cell, icon_cell = CalloutLayout.SINGLE_CELL, cells[0], None
    elif len(cells) == 2 and is_image_only(cells[0]) and block_text(cells[1]):
        layout, text_cell, icon_cell = CalloutLayout.ICON_TEXT_TWO_CELL, cells[1], cells[0]
    else:
        return None
    text = block_text(text_cell)
    if not text and icon_cell is None:
        return None

    colours = [c.get(qn("w:val")).upper() for c in text_cell.iter(qn("w:color")) if c.get(qn("w:val"))]
    blue = TemplateDocxParser.BLUE_HEX_VALUES
    non_blue = [c for c in colours if c not in blue and c != "AUTO"]
    return _Box(
        table_index=table_index,
        fill_hex=fill,
        layout=layout,
        text=text,
        icon_key=icon_key_for(doc, icon_cell if icon_cell is not None else text_cell),
        text_color_hex=non_blue[0] if non_blue else None,
        column_widths_pt=grid_widths_pt(tbl),
        is_instruction=bool(colours) and not non_blue,
    )


def is_image_only(tc) -> bool:
    has_image = next(tc.iter(qn("w:drawing")), None) is not None or next(tc.iter(qn("w:pict")), None) is not None
    return has_image and not block_text(tc)


def icon_key_for(doc, el) -> Optional[str]:
    """Same key the parser gives the image: ``icon_`` + md5 prefix of its bytes."""
    if el is None:
        return None
    for blip in el.iter(_A_BLIP):
        rel = doc.part.rels.get(blip.get(_R_EMBED))
        if rel is not None and "image" in rel.reltype:
            return "icon_" + hashlib.md5(rel.target_part.blob).hexdigest()[:10]
    return None


def grid_widths_pt(tbl) -> list[float]:
    widths = []
    grid = tbl.find(qn("w:tblGrid"))
    # Direct children only: a tracked w:tblGridChange holds the previous grid.
    for col in grid.findall(qn("w:gridCol")) if grid is not None else []:
        try:
            widths.append(round(int(col.get(qn("w:w"))) / 20, 1))
        except (TypeError, ValueError):
            pass
    return widths


def _legend_label(text: str) -> str:
    """"Explanation – additional information..." → "Explanation"."""
    label = re.split(r"\s+[–—-]\s+|\s*\(", text, maxsplit=1)[0].strip()
    return label[:80] or text[:80]


# ── Highlights ─────────────────────────────────────────────────────────

def _highlights(doc) -> tuple[list[HighlightedText], list[str]]:
    found: list[HighlightedText] = []
    optional: list[str] = []
    for el in iter_body_blocks(doc.element.body):
        paragraphs = [el] if el.tag == qn("w:p") else list(el.iter(qn("w:p")))
        for p in paragraphs:
            if style_name(doc, p).lower().startswith("toc"):
                continue  # TOC entries outside a TOC content control
            heading = block_text(p) if el.tag == qn("w:p") and is_heading(doc, p) else None
            for r in p.iter(qn("w:r")):
                hl = r.find(f"{qn('w:rPr')}/{qn('w:highlight')}")
                if hl is None or (hl.get(qn("w:val")) or "none") == "none":
                    continue
                run_text = "".join(t.text or "" for t in r.iter(qn("w:t")))
                if not run_text.strip():
                    continue
                found.append(HighlightedText(block_text(p), run_text, hl.get(qn("w:val")), heading))
                if heading and "optional" in run_text.lower() and heading not in optional:
                    optional.append(heading)
    return found, optional

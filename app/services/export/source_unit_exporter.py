"""Map the document AST to the v2 ``SourceDocument``: ordered sections and traceable units.

Runs next to ``MigrationExporter`` (v3.1); it reads the same AST and writes a
separate file, so the existing output is unchanged.

What becomes a unit:
- a paragraph (typed warning, note or caption by its prefix);
- each list item (procedure step when numbered, bullet otherwise), with its Word level;
- each table row with all its cells; header rows feed ``header_cells``;
- each paragraph of an "icon table" (image-only cell beside a text cell), with
  the icon as an asset. These tables are layout, not data;
- a figure (a picture larger than an icon), with its caption linked.

Icons are metadata (``SourceAsset`` of kind ``icon``) on the unit they sit
beside. They never become content, and a figure never becomes an icon.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from app.schemas.ast_nodes import DocumentNode
from app.schemas.v2 import (
    AssetKind,
    RelationType,
    SourceAsset,
    SourceDocument,
    SourceLocation,
    SourceSection,
    SourceUnit,
    TableCell,
    TableRef,
    UnitRelation,
    UnitType,
    compute_content_hash,
)
from app.services.export.migration_exporter import MigrationExporter
from app.services.export.source_refs import detect_cross_references

# A picture no larger than this (either side, px at 96 dpi) is an icon.
ICON_MAX_PX = 96

_CAPTION_RE = re.compile(r"^(?:figure|fig\.|image|table|abbildung)\s*\d+\s*[:.\-–]", re.I)
_WARNING_RE = re.compile(r"^(?:warning|caution|danger|attention|important)\b\s*[:!\-–]", re.I)
_NOTE_RE = re.compile(r"^(?:note|remark|hint|n\.?b\.?)\b\s*[:\-–]", re.I)
_BOILERPLATE_HEADING_RE = re.compile(
    r"\b(?:document|revision|change|version)\s+history\b|\bapprovals?\b|\bsignatures?\b", re.I
)
_REFERENCE_HEADING_RE = re.compile(r"\breferences?\b|\bassociated\b.*\bdocuments?\b", re.I)
_DEFINITION_HEADER_RE = re.compile(r"^(?:term|abbreviation|acronym)s?$", re.I)


def _is_grey(fill: Optional[str]) -> bool:
    """Neutral grey fills (D9D9D9, F2F2F2, BFBFBF...) mark header rows, not meaning."""
    if not fill or len(fill) != 6:
        return False
    r, g, b = fill[0:2], fill[2:4], fill[4:6]
    return r == g == b and int(r, 16) >= 0xA0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class _Builder:
    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        self.sections: list[SourceSection] = []
        self.seq = 0
        self.pending_icons: list[SourceAsset] = []
        self.figure_by_node: dict[str, str] = {}
        self._dup: Counter = Counter()
        self._table_count: Counter = Counter()

    # Sections ---------------------------------------------------------

    def open_section(self, number: Optional[str], heading: str, level: int, parent_id: Optional[str]) -> SourceSection:
        order = len(self.sections)
        base = f"SRC-{number}" if number else f"SRC-S{order:02d}"
        section_id = base
        suffix = 2
        existing = {s.section_id for s in self.sections}
        while section_id in existing:
            section_id = f"{base}-{suffix}"
            suffix += 1
        section = SourceSection(
            section_id=section_id, number=number, heading=heading, level=level,
            section_order=order, parent_id=parent_id,
        )
        self.sections.append(section)
        return section

    # Units ------------------------------------------------------------

    def add_unit(
        self,
        section: SourceSection,
        unit_type: UnitType,
        text: str,
        node: Any = None,
        boilerplate: bool = False,
        **fields,
    ) -> SourceUnit:
        text = _norm(text)
        key = (section.section_id, text.lower())
        self._dup[key] += 1
        assets = list(fields.pop("assets", []))
        if unit_type != UnitType.FIGURE and self.pending_icons:
            assets = self.pending_icons + assets
            self.pending_icons = []
        loc = getattr(node, "source_location", None)
        unit = SourceUnit(
            unit_id=f"{section.section_id}-U{len(section.units) + 1:03d}",
            content_hash=compute_content_hash(text, f"{section.heading}#{self._dup[key]}"),
            section_id=section.section_id,
            seq=self.seq,
            unit_type=unit_type,
            text=text,
            location=SourceLocation(
                page=getattr(loc, "page", None),
                paragraph_index=getattr(loc, "paragraph_index", None),
            ),
            assets=assets,
            is_boilerplate=boilerplate,
            **fields,
        )
        self.seq += 1
        section.units.append(unit)
        return unit

    def next_table_id(self, section: SourceSection) -> str:
        self._table_count[section.section_id] += 1
        return f"{section.section_id}-T{self._table_count[section.section_id]}"


class SourceUnitExporter:
    """AST → ``SourceDocument``."""

    @classmethod
    def export(
        cls,
        document_id: str,
        ast: DocumentNode,
        source_file: str = "",
        file_type: Optional[str] = None,
    ) -> SourceDocument:
        meta = ast.doc_metadata
        b = _Builder(document_id)
        preamble: Optional[SourceSection] = None

        for child in ast.children:
            if getattr(child, "node_type", None) == "section":
                cls._section(b, child, parent_id=None, inherited_boilerplate=False)
            else:
                if preamble is None:
                    preamble = b.open_section("0", "PREAMBLE", 0, None)
                cls._content(b, preamble, child, boilerplate=True)

        cls._link_captions(b)
        cls._link_warnings(b)

        doc = SourceDocument(
            document_id=document_id,
            document_version=getattr(meta, "document_version", None) or None,
            source_file=Path(source_file).name if source_file else "",
            file_type=file_type or getattr(meta, "file_type", None) or "docx",
            language=getattr(meta, "language", None) or "en",
            sections=b.sections,
        )
        doc.cross_references = detect_cross_references(doc)
        return doc

    # ── Walking ───────────────────────────────────────────────────────

    @classmethod
    def _section(cls, b: _Builder, node: Any, parent_id: Optional[str], inherited_boilerplate: bool) -> None:
        heading = getattr(node, "heading", None)
        text = _norm(getattr(heading, "text", "") or "")
        number = getattr(heading, "numbering", None)
        if number and re.match(rf"^{re.escape(number)}(?:[.\s]|$)", text):
            text = text[len(number):].lstrip(". ").strip()
        level = getattr(node, "level", None) or getattr(heading, "level", 1) or 1
        section = b.open_section(number, text or "(untitled)", level, parent_id)
        boilerplate = inherited_boilerplate or bool(_BOILERPLATE_HEADING_RE.search(text))
        for child in getattr(node, "children", []) or []:
            if getattr(child, "node_type", None) == "section":
                cls._section(b, child, parent_id=section.section_id, inherited_boilerplate=boilerplate)
            else:
                cls._content(b, section, child, boilerplate=boilerplate)

    @classmethod
    def _content(cls, b: _Builder, section: SourceSection, node: Any, boilerplate: bool) -> None:
        if MigrationExporter._is_running_header_or_footer(node):
            return
        kind = getattr(node, "node_type", None)
        if kind == "paragraph":
            text = getattr(node, "text", "")
            if _norm(text):
                b.add_unit(section, cls._paragraph_type(text), text, node, boilerplate)
        elif kind == "list":
            ordered = getattr(node, "list_type", "") == "ordered"
            for item in getattr(node, "items", []) or []:
                meta = getattr(item, "metadata", None) or {}
                number = meta.get("list_number") or (str(item.index) if ordered and item.index else None)
                b.add_unit(
                    section,
                    UnitType.PROCEDURE_STEP if ordered else UnitType.BULLET,
                    item.text, item, boilerplate,
                    list_level=meta.get("list_level", 0),
                    list_number=number,
                )
        elif kind == "table":
            cls._table(b, section, node, boilerplate)
        elif kind == "image":
            cls._image(b, section, node, boilerplate)
        elif kind == "icon":
            cls._attach_icon(b, section, cls._icon_asset(node), node)
        elif kind == "caption":
            text = getattr(node, "text", "")
            if _norm(text):
                unit = b.add_unit(section, UnitType.CAPTION, text, node, boilerplate)
                target = b.figure_by_node.get(getattr(node, "referenced_node_id", None) or "")
                if target:
                    unit.relations.append(UnitRelation(type=RelationType.CAPTION_OF, target_unit_id=target))
        elif kind == "heading":
            text = getattr(node, "text", "")
            if _norm(text):
                b.add_unit(section, UnitType.HEADING_STATEMENT, text, node, boilerplate)

    @staticmethod
    def _paragraph_type(text: str) -> UnitType:
        stripped = _norm(text)
        if _CAPTION_RE.match(stripped):
            return UnitType.CAPTION
        if _WARNING_RE.match(stripped):
            return UnitType.WARNING
        if _NOTE_RE.match(stripped):
            return UnitType.NOTE
        return UnitType.PARAGRAPH

    # ── Media ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_icon_size(node: Any) -> bool:
        w, h = getattr(node, "width", 0) or 0, getattr(node, "height", 0) or 0
        return 0 < max(w, h) <= ICON_MAX_PX

    @staticmethod
    def _asset_key(node: Any) -> str:
        digest = getattr(node, "image_hash", None) or getattr(node, "content_hash", None)
        if digest:
            return f"icon_{digest}"
        return f"icon_{Path(getattr(node, 'asset_path', '') or 'unknown').stem}"

    @classmethod
    def _icon_asset(cls, node: Any) -> SourceAsset:
        meta = getattr(node, "metadata", None) or {}
        return SourceAsset(
            kind=AssetKind.ICON,
            asset_key=cls._asset_key(node),
            path=getattr(node, "asset_path", None) or None,
            width_px=getattr(node, "width", None) or meta.get("width_px") or None,
            height_px=getattr(node, "height", None) or meta.get("height_px") or None,
        )

    @staticmethod
    def _attach_icon(b: _Builder, section: SourceSection, asset: SourceAsset, node: Any) -> None:
        """An icon inline with text belongs to that text; otherwise to the next unit."""
        index = getattr(getattr(node, "source_location", None), "paragraph_index", None)
        last = section.units[-1] if section.units else None
        if index is not None and last is not None and last.location.paragraph_index == index:
            last.assets.append(asset)
        else:
            b.pending_icons.append(asset)

    @classmethod
    def _image(cls, b: _Builder, section: SourceSection, node: Any, boilerplate: bool) -> None:
        if cls._is_icon_size(node):
            cls._attach_icon(b, section, cls._icon_asset(node), node)
            return
        caption = _norm(getattr(node, "caption", None) or "")
        alt = _norm(getattr(node, "alt_text", "") or "")
        asset = SourceAsset(
            kind=AssetKind.FIGURE,
            asset_key=cls._asset_key(node),
            path=getattr(node, "asset_path", None) or None,
            width_px=getattr(node, "width", None) or None,
            height_px=getattr(node, "height", None) or None,
        )
        # The figure unit names the picture; its caption, when the caption extractor
        # attached one, becomes its own CAPTION unit so every caption is a unit.
        label = alt if alt and alt != caption else "[Figure]"
        unit = b.add_unit(section, UnitType.FIGURE, label, node, boilerplate, assets=[asset])
        b.figure_by_node[getattr(node, "node_id", "")] = unit.unit_id
        if caption:
            b.add_unit(
                section, UnitType.CAPTION, caption, node, boilerplate,
                relations=[UnitRelation(type=RelationType.CAPTION_OF, target_unit_id=unit.unit_id)],
            )

    # ── Tables ────────────────────────────────────────────────────────

    @staticmethod
    def _cell_parts(cell: Any) -> tuple[str, list[Any]]:
        """(text, media nodes) of a table cell."""
        texts, media = [], []
        for item in getattr(cell, "content", []) or []:
            kind = getattr(item, "node_type", None)
            if kind in ("image", "icon"):
                media.append(item)
            elif getattr(item, "text", None):
                texts.append(item.text)
        return "\n".join(texts).strip(), media

    @classmethod
    def _is_icon_table(cls, rows: list[Any]) -> bool:
        """Every row: an image-only cell beside text. Layout for icon + text, not data."""
        if not rows:
            return False
        for row in rows:
            parts = [cls._cell_parts(c) for c in row.cells if c.is_merge_origin]
            image_only = [p for p in parts if p[1] and not p[0]]
            with_text = [p for p in parts if p[0]]
            if not image_only or not with_text:
                return False
            if any(not cls._is_icon_size(m) and (getattr(m, "width", 0) or 0) > 0 for p in image_only for m in p[1]):
                return False
        return True

    @classmethod
    def _table(cls, b: _Builder, section: SourceSection, node: Any, boilerplate: bool) -> None:
        rows = list(getattr(node, "rows", []) or [])
        if not rows:
            return
        table_id = b.next_table_id(section)

        if cls._is_icon_table(rows):
            for row in rows:
                icons, paragraphs, cells = [], [], []
                for cell in row.cells:
                    if not cell.is_merge_origin:
                        continue
                    text, media = cls._cell_parts(cell)
                    icons += [cls._icon_asset(m) for m in media]
                    if text:
                        cells.append(TableCell(col=cell.col_index, text=_norm(text)))
                        paragraphs += [p for p in text.split("\n") if p.strip()]
                for i, para in enumerate(paragraphs):
                    b.add_unit(
                        section, cls._paragraph_type(para), para, row, boilerplate,
                        assets=icons if i == 0 else [],
                        table_ref=TableRef(table_id=table_id, row_index=row.row_index, cells=cells),
                    )
            return

        header_count = cls._header_row_count(node, rows)
        headers = cls._header_texts(rows[:header_count])
        data_rows = rows[header_count:] or rows[:header_count]
        is_definition = bool(headers) and bool(_DEFINITION_HEADER_RE.match(headers[0] or ""))
        is_reference = bool(_REFERENCE_HEADING_RE.search(section.heading))

        for row in data_rows:
            cells, figures, icons = [], [], []
            fills = [c.shading_hex for c in row.cells if c.is_merge_origin]
            uniform_grey = len(set(fills)) == 1 and _is_grey(fills[0])
            for cell in row.cells:
                if not cell.is_merge_origin:
                    continue
                text, media = cls._cell_parts(cell)
                for m in media:
                    (icons if cls._is_icon_size(m) or getattr(m, "node_type", "") == "icon" else figures).append(m)
                fill = cell.shading_hex
                keep_fill = fill and not uniform_grey and not getattr(row, "is_header", False) and not _is_grey(fill)
                lines = [line for line in (_norm(t) for t in text.split("\n")) if line]
                cells.append(TableCell(
                    col=cell.col_index, text=_norm(text), row_span=cell.row_span, col_span=cell.col_span,
                    is_header=row in rows[:header_count], fill_hex=fill if keep_fill else None,
                    paragraphs=lines if len(lines) > 1 else [],
                ))
            row_text = " | ".join(c.text for c in cells if c.text)
            if row_text:
                unit_type = UnitType.DEFINITION if is_definition else (
                    UnitType.REFERENCE if is_reference else UnitType.TABLE_ROW)
                b.add_unit(
                    section, unit_type, row_text, row, boilerplate,
                    assets=[cls._icon_asset(m) for m in icons],
                    table_ref=TableRef(table_id=table_id, row_index=row.row_index, header_cells=headers, cells=cells),
                )
            for m in figures:
                cls._image(b, section, m, boilerplate)

    @staticmethod
    def _header_row_count(node: Any, rows: list[Any]) -> int:
        declared = getattr(node, "header_rows", 0) or 0
        if declared:
            return min(declared, len(rows))
        if len(rows) < 2:
            return 0
        first = [c for c in rows[0].cells if c.is_merge_origin]
        texts = [SourceUnitExporter._cell_parts(c)[0] for c in first]
        if not any(texts):
            return 0
        filled = [c.shading_hex for c in first]
        all_bold = all(c.bold for c, t in zip(first, texts) if t)
        same_fill = len(set(filled)) == 1 and filled[0] is not None
        return 1 if all_bold or same_fill else 0

    @staticmethod
    def _header_texts(header_rows: list[Any]) -> list[str]:
        if not header_rows:
            return []
        width = max(len(r.cells) for r in header_rows)
        cols = []
        for i in range(width):
            parts = []
            for r in header_rows:
                if i < len(r.cells) and r.cells[i].is_merge_origin:
                    t = _norm(SourceUnitExporter._cell_parts(r.cells[i])[0])
                    if t and t not in parts:
                        parts.append(t)
            cols.append(" / ".join(parts))
        return cols

    # ── Post-passes ───────────────────────────────────────────────────

    @staticmethod
    def _link_captions(b: _Builder) -> None:
        """A caption paragraph next to a figure describes it ("Image 1: ..." after the picture)."""
        for section in b.sections:
            units = section.units
            for i, unit in enumerate(units):
                if unit.unit_type != UnitType.CAPTION or unit.relations:
                    continue
                for j in (i - 1, i + 1):
                    if 0 <= j < len(units) and units[j].unit_type == UnitType.FIGURE:
                        unit.relations.append(UnitRelation(type=RelationType.CAPTION_OF, target_unit_id=units[j].unit_id))
                        break

    @staticmethod
    def _link_warnings(b: _Builder) -> None:
        """A warning or note qualifies the content before it, or the next unit when it comes first."""
        skip = {UnitType.WARNING, UnitType.NOTE, UnitType.CAPTION, UnitType.FIGURE}
        for section in b.sections:
            units = section.units
            for i, unit in enumerate(units):
                if unit.unit_type not in (UnitType.WARNING, UnitType.NOTE):
                    continue
                rel = RelationType.WARNING_FOR if unit.unit_type == UnitType.WARNING else RelationType.NOTE_FOR
                before = next((u for u in reversed(units[:i]) if u.unit_type not in skip), None)
                after = next((u for u in units[i + 1:] if u.unit_type not in skip), None)
                target = before or after
                if target is not None:
                    unit.relations.append(UnitRelation(type=rel, target_unit_id=target.unit_id))

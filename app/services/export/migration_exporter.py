"""Migration Exporter for Clean .docx-Ready Output.

This module converts the extracted AST (DocumentNode) and AssetManifest into a
concise, section-wise reading-order JSON structure (DocxMigrationOutput) designed
specifically for reconstructing Microsoft Word (.docx) documents.
"""

from __future__ import annotations

import re
from typing import Optional, Any

from loguru import logger

from app.schemas.ast_nodes import (
    DocumentNode,
    SectionNode,
    HeadingNode,
    ParagraphNode,
    ListNode,
    ListItemNode,
    TableNode,
    TableRowNode,
    TableCellNode,
    ImageNode,
    IconNode,
    CaptionNode,
)
from app.schemas.document import BoundingBox, ExtractedImage, ExtractedTableCell
from app.schemas.migration import (
    DocxMigrationOutput,
    MigrationMetadata,
    MigrationSection,
    MigrationElement,
    MigrationTableCell,
    MigrationIconRef,
)
from app.services.extraction.tables import (
    collapse_sparse_grid,
    clean_table_cell_text,
)


def _is_major_section_heading(
    title: str,
    level: Optional[int] = None,
    current_section_number: Optional[str] = None,
) -> Optional[str]:
    stripped = title.strip()
    if level is not None and level > 1:
        return None
    if stripped.endswith(":") or re.match(r"^[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25AB\u25CF\u25CB\u25A0\u25A1\u2013\u2014○●◆◇■□▪▫–—•‣⁃]", stripped):
        return None
    # Subsections like 6.1, 6.5.1 are not major sections
    if re.match(r"^\d+\.\d+", stripped):
        return None
    match = re.match(r"^(\d+)\s+([A-Z0-9\s&/\-_,]+)$", stripped)
    if match:
        sec_num = match.group(1)
        title_part = match.group(2).strip()
        if title_part in ("N/A", "NONE", "NO.") or title_part.startswith("BI-"):
            return None
        if current_section_number is not None and current_section_number.isdigit() and sec_num.isdigit():
            curr_n = int(current_section_number)
            new_n = int(sec_num)
            if new_n < curr_n:
                return None
        return sec_num
    return None


def _word_numbered_heading(
    heading: Any,
    text: str,
    level: int,
    sections: list,
) -> tuple[str, Optional[str]]:
    """Return (display text, major section number or None) for a DOCX heading.

    Word auto-numbering is not part of the heading text, so the parser computes
    it ("6", "6.1.2") and stores it as ``numbering`` with
    ``metadata["numbering_source"] == "numPr"``. The number is prefixed to the
    text so titles keep the "6 PROCESS" form downstream code expects. A level-1
    heading with a top-level number starts a section. A "Heading 1" with no
    number at all gets the next running number.
    """
    if heading is None or not text:
        return text, None
    meta = getattr(heading, "metadata", None) or {}
    number = getattr(heading, "numbering", None) if meta.get("numbering_source") == "numPr" else None
    if text.endswith(":"):
        return text, None
    if number:
        display = text if re.match(rf"^{re.escape(number)}(?:[.\s]|$)", text) else f"{number} {text}"
        major = number if level == 1 and "." not in number else None
        return display, major
    style = (getattr(heading, "style_name", None) or "").lower()
    if level == 1 and style == "heading 1" and not re.match(r"^\d", text):
        used = [int(sec.section_number) for sec in sections if (sec.section_number or "").isdigit()]
        major = str(max(used, default=0) + 1)
        return f"{major} {text}", major
    return text, None


def _extract_section_number(title: str) -> Optional[str]:
    match = re.match(r"^(\d+(?:\.\d+)*)", title.strip())
    if match:
        return match.group(1).rstrip(".")
    return None


class MigrationExporter:
    """Exports AST and assets to a Clean .docx Document Migration format."""

    @classmethod
    def export(
        cls,
        document_id: str,
        ast: DocumentNode,
        assets_manifest: Optional[Any] = None,
    ) -> DocxMigrationOutput:
        """Convert a DocumentNode AST into a clean DocxMigrationOutput envelope."""
        metadata = cls._extract_metadata(document_id, ast)
        sections = cls._traverse_ast(ast)

        return DocxMigrationOutput(
            version="3.1",
            document_id=document_id,
            metadata=metadata,
            sections=sections,
        )

    @classmethod
    def _is_running_header_or_footer(cls, node: Any) -> bool:
        """Fallback check to suppress running header and footer nodes."""
        node_type = getattr(node, "node_type", None)
        if node_type in ("document", "section", "table"):
            return False
        text = (getattr(node, "text", "") or "").strip()
        if not text:
            return False

        import re
        patterns = [
            r"^Number:\s*BI-VQD-",
            r"^BI-VQD-\d+",
            r"^Boehringer\s+Ingelheim\s*Page\s+\d+",
            r"^Title:\s*Good\s+Writing\s+Practice",
            r"^Document\s+Name:\s*BI-VQD-",
            r"^Version:\s*\d+",
            r"^Effective\s+Date:\s*",
            r"^Life\s+forward$",
            r"Property\s+of\s+Boehringer\s+Ingelheim",
            r"Retrieved\s+by\s+",
            r"Verify\s+the\s+current\s+version",
            r"Working\s+Copy",
        ]
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    @classmethod
    def _extract_metadata(cls, document_id: str, ast: DocumentNode) -> MigrationMetadata:
        doc_meta = getattr(ast, "doc_metadata", None)
        if not doc_meta:
            return MigrationMetadata(document_id=document_id)

        gpdat_version = (
            getattr(doc_meta, "gpdat_version", 0)
            or getattr(doc_meta, "duplicate_upload_count", 0)
        )
        return MigrationMetadata(
            document_id=document_id,
            document_number=getattr(doc_meta, "document_number", None),
            document_name=getattr(doc_meta, "document_name", None),
            document_title=getattr(doc_meta, "document_title", None) or None,
            document_version=getattr(doc_meta, "document_version", None),
            document_type=getattr(doc_meta, "document_type", None) or None,
            file_type=getattr(doc_meta, "file_type", "") or "",
            language=getattr(doc_meta, "language", "en"),
            page_count=getattr(doc_meta, "page_count", 0),
            gpdat_version=gpdat_version,
        )

    @classmethod
    def _get_page(cls, node: Any) -> int:
        source_loc = getattr(node, "source_location", None)
        if source_loc and getattr(source_loc, "page", None) is not None:
            return source_loc.page
        return 0

    @classmethod
    def _node_bbox(cls, node: Any) -> Optional[BoundingBox]:
        loc = getattr(node, "source_location", None)
        if loc is None:
            return None
        return getattr(loc, "bbox", None)

    @staticmethod
    def _y_overlap_ratio(a: BoundingBox, b: BoundingBox) -> float:
        if a.page != b.page:
            return 0.0
        overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
        if overlap <= 0:
            return 0.0
        min_h = min(a.y1 - a.y0, b.y1 - b.y0)
        if min_h <= 0:
            return 0.0
        return overlap / min_h

    @classmethod
    def _traverse_ast(cls, ast: DocumentNode) -> list[MigrationSection]:
        sections: list[MigrationSection] = []
        current_section: Optional[MigrationSection] = None
        buffered_icons: list[tuple[MigrationIconRef, Optional[BoundingBox]]] = []
        elem_bboxes: dict[int, BoundingBox] = {}
        icon_bboxes: dict[int, BoundingBox] = {}

        def ensure_section(title: str, page: int, section_number: Optional[str] = None) -> MigrationSection:
            nonlocal current_section
            if section_number is None:
                section_number = _extract_section_number(title)
            sec = MigrationSection(
                section_number=section_number,
                title=title,
                page_start=page,
                page_end=page,
                elements=[],
            )
            sections.append(sec)
            current_section = sec
            return sec

        def flush_icons_onto(elem: MigrationElement):
            if not buffered_icons:
                return
            for icon_ref, bbox in buffered_icons:
                elem.icons.append(icon_ref)
                if bbox is not None:
                    icon_bboxes[id(icon_ref)] = bbox
            buffered_icons.clear()

        def add_element(elem: MigrationElement, bbox: Optional[BoundingBox] = None):
            nonlocal current_section
            if current_section is None:
                ensure_section("0 PREAMBLE", elem.page, section_number="0")

            flush_icons_onto(elem)
            if bbox is not None:
                elem_bboxes[id(elem)] = bbox

            current_section.elements.append(elem)
            current_section.page_end = max(current_section.page_end, elem.page)

        def attach_icon(icon_ref: MigrationIconRef, bbox: Optional[BoundingBox] = None):
            # Left-rail icons precede the text they annotate in reading order.
            buffered_icons.append((icon_ref, bbox))

        def traverse(node: Any, is_top_section: bool = False):
            if node is None:
                return

            if cls._is_running_header_or_footer(node):
                return

            node_type = getattr(node, "node_type", None)
            page = cls._get_page(node)
            bbox = cls._node_bbox(node)

            if node_type == "document":
                for child in getattr(node, "children", []):
                    traverse(child, is_top_section=True)

            elif node_type == "section":
                heading = getattr(node, "heading", None)
                heading_level = getattr(heading, "level", 1) if heading else 1
                heading_text = (getattr(heading, "text", "") or "").strip() if heading else ""
                heading_text, major_sec = _word_numbered_heading(heading, heading_text, heading_level, sections)
                if major_sec is None and heading_text:
                    curr_sec_num = current_section.section_number if current_section else None
                    major_sec = _is_major_section_heading(
                        heading_text, level=heading_level, current_section_number=curr_sec_num
                    )

                if major_sec:
                    ensure_section(heading_text, page, section_number=major_sec)
                else:
                    if current_section is None:
                        ensure_section("0 PREAMBLE", page, section_number="0")
                    if heading and heading_text:
                        h_stripped = heading_text.replace("\u200b", "").strip()
                        if (
                            re.match(r"^[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25AB\u25CF\u25CB\u25A0\u25A1\u2013\u2014○●◆◇■□▪▫–—•‣⁃]", h_stripped)
                            or (re.match(r"^(?:\d{1,3}[.)]\s|[a-zA-Z][.)]\s)", h_stripped) and h_stripped.endswith(":"))
                        ):
                            add_element(
                                MigrationElement(
                                    element_type="list",
                                    page=page,
                                    items=[heading_text],
                                ),
                                bbox=bbox,
                            )
                        else:
                            add_element(
                                MigrationElement(
                                    element_type="heading",
                                    page=page,
                                    level=max(2, heading_level),
                                    text=heading_text,
                                ),
                                bbox=bbox,
                            )

                for child in getattr(node, "children", []):
                    traverse(child, is_top_section=False)

            elif node_type == "heading":
                text = (getattr(node, "text", "") or "").strip()
                if text:
                    level = getattr(node, "level", 1)
                    text, major_sec = _word_numbered_heading(node, text, level, sections)
                    if major_sec is None:
                        curr_sec_num = current_section.section_number if current_section else None
                        major_sec = _is_major_section_heading(
                            text, level=level, current_section_number=curr_sec_num
                        )
                    if major_sec:
                        ensure_section(text, page, section_number=major_sec)
                        add_element(
                            MigrationElement(
                                element_type="heading",
                                page=page,
                                level=1,
                                text=text,
                            ),
                            bbox=bbox,
                        )
                    else:
                        if current_section is None:
                            ensure_section("0 PREAMBLE", page, section_number="0")
                        h_stripped = text.replace("\u200b", "").strip()
                        if (
                            re.match(r"^[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25AB\u25CF\u25CB\u25A0\u25A1\u2013\u2014○●◆◇■□▪▫–—•‣⁃]", h_stripped)
                            or (re.match(r"^(?:\d{1,3}[.)]\s|[a-zA-Z][.)]\s)", h_stripped) and h_stripped.endswith(":"))
                        ):
                            add_element(
                                MigrationElement(
                                    element_type="list",
                                    page=page,
                                    items=[text],
                                ),
                                bbox=bbox,
                            )
                        else:
                            add_element(
                                MigrationElement(
                                    element_type="heading",
                                    page=page,
                                    level=max(2, level),
                                    text=text,
                                ),
                                bbox=bbox,
                            )

            elif node_type == "paragraph":
                text = (getattr(node, "text", "") or "").strip()
                if text:
                    add_element(
                        MigrationElement(
                            element_type="paragraph",
                            page=page,
                            text=text,
                        ),
                        bbox=bbox,
                    )

            elif node_type == "list":
                items: list[str] = []
                list_type = getattr(node, "list_type", "") or ""
                for item in getattr(node, "items", []):
                    item_text = (getattr(item, "text", "") or "").strip()
                    if not item_text:
                        continue
                    idx = getattr(item, "index", None)
                    if str(list_type) == "ordered" and idx:
                        items.append(f"{idx}. {item_text}")
                    else:
                        items.append(item_text)
                if items:
                    add_element(
                        MigrationElement(
                            element_type="list",
                            page=page,
                            items=items,
                        ),
                        bbox=bbox,
                    )

            elif node_type == "table":
                table_elem = cls._convert_table(node, page)
                if table_elem:
                    add_element(table_elem, bbox=bbox)

            elif node_type == "image":
                path = getattr(node, "asset_path", "")
                if path:
                    caption = getattr(node, "caption", None) or getattr(node, "alt_text", None)
                    add_element(
                        MigrationElement(
                            element_type="image",
                            page=page,
                            title=caption,
                            image_path=path,
                        ),
                        bbox=bbox,
                    )

            elif node_type == "icon":
                path = getattr(node, "asset_path", "")
                if path:
                    icon_id = getattr(node, "node_id", "") or "icon"
                    attach_icon(
                        MigrationIconRef(
                            icon_id=str(icon_id),
                            path=path,
                            semantic_meaning=getattr(node, "semantic_meaning", None),
                        ),
                        bbox=bbox,
                    )

            elif hasattr(node, "children"):
                for child in getattr(node, "children", []):
                    traverse(child, is_top_section=False)

        traverse(ast, is_top_section=True)

        if buffered_icons and current_section and current_section.elements:
            flush_icons_onto(current_section.elements[-1])

        cls._reclaim_section_continuations(sections)
        cls._rebind_icons_by_y(sections, elem_bboxes, icon_bboxes)
        cls._spread_icons(sections)
        return sections

    @staticmethod
    def _looks_incomplete_fragment(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if t.endswith((".", "!", "?", ":", ";", "…")):
            return False
        return True

    @staticmethod
    def _looks_sentence_continuation(text: str) -> bool:
        t = (text or "").strip()
        return bool(t) and t[0].islower()

    @classmethod
    def _reclaim_section_continuations(cls, sections: list[MigrationSection]) -> None:
        """Move a leftover lowercase paragraph back when a heading split a sentence.

        SOP left-rail layouts often emit ``This SOP`` then the next heading, then
        ``defines the …`` which still belongs to the previous section.
        """
        i = 0
        while i < len(sections) - 1:
            curr, nxt = sections[i], sections[i + 1]
            last_para = next(
                (e for e in reversed(curr.elements) if e.element_type == "paragraph" and e.text),
                None,
            )
            if not last_para or not cls._looks_incomplete_fragment(last_para.text):
                i += 1
                continue

            moved = False
            for elem in list(nxt.elements):
                if elem.element_type == "heading":
                    continue
                if elem.element_type == "paragraph" and elem.text:
                    if last_para.page != elem.page or last_para.page == 0:
                        break
                    if cls._looks_sentence_continuation(elem.text):
                        nxt.elements.remove(elem)
                        curr.elements.append(elem)
                        curr.page_end = max(curr.page_end, elem.page)
                        moved = True
                        break
                    # Lead-in like "This SOP is applicable:" — keep looking.
                    if elem.text.strip().endswith(":"):
                        continue
                    break
                break
            if not moved:
                i += 1

    @classmethod
    def _rebind_icons_by_y(
        cls,
        sections: list[MigrationSection],
        elem_bboxes: dict[int, BoundingBox],
        icon_bboxes: dict[int, BoundingBox],
    ) -> None:
        """Re-attach icons to the paragraph they vertically overlap when bboxes exist."""
        if not icon_bboxes or not elem_bboxes:
            return

        targets = [
            e
            for s in sections
            for e in s.elements
            if e.element_type in ("paragraph", "list")
        ]
        relocatable: list[MigrationIconRef] = []
        for elem in targets:
            keep: list[MigrationIconRef] = []
            for ic in elem.icons:
                if id(ic) in icon_bboxes:
                    relocatable.append(ic)
                else:
                    keep.append(ic)
            elem.icons = keep

        relocatable.sort(
            key=lambda ic: (
                icon_bboxes[id(ic)].page,
                icon_bboxes[id(ic)].y0,
                icon_bboxes[id(ic)].x0,
            )
        )
        used: set[int] = {id(e) for e in targets if e.icons}
        for ic in relocatable:
            bb = icon_bboxes[id(ic)]
            best: Optional[MigrationElement] = None
            best_score: tuple[float, float, int] | None = None
            for elem in targets:
                eb = elem_bboxes.get(id(elem))
                if eb is None:
                    continue
                overlap = cls._y_overlap_ratio(bb, eb)
                if overlap < 0.25:
                    continue
                if eb.x1 < bb.x0:
                    continue
                unused_bonus = 1 if id(elem) not in used else 0
                hdist = max(0.0, eb.x0 - bb.x1)
                score = (unused_bonus, overlap, -hdist)
                if best_score is None or score > best_score:
                    best_score = score
                    best = elem
            if best is None:
                continue
            best.icons.append(ic)
            used.add(id(best))

    @staticmethod
    def _spread_icons(sections: list[MigrationSection]) -> None:
        """If several left-rail icons flushed onto one element, give extras to following empty siblings."""
        for sec in sections:
            elems = sec.elements
            for i, elem in enumerate(elems):
                extras = elem.icons[1:]
                if not extras:
                    continue
                elem.icons = elem.icons[:1]
                dest = i + 1
                for icon in extras:
                    while dest < len(elems) and elems[dest].icons:
                        dest += 1
                    if dest >= len(elems):
                        elem.icons.append(icon)
                        continue
                    elems[dest].icons.append(icon)
                    dest += 1

    @classmethod
    def _convert_table(cls, table: TableNode, page: int) -> Optional[MigrationElement]:
        rows = getattr(table, "rows", [])
        if not rows:
            return None

        num_rows = len(rows)
        # Determine num_cols from grid_cols or max cells across rows
        num_cols = getattr(table, "grid_cols", 0)
        if not num_cols:
            max_c = 0
            for r in rows:
                c_count = 0
                for cell in getattr(r, "cells", []):
                    c_count += getattr(cell, "col_span", 1)
                max_c = max(max_c, c_count)
            num_cols = max_c

        cells: list[MigrationTableCell] = []

        for row_idx, row in enumerate(rows):
            is_header = getattr(row, "is_header", False)
            for cell in getattr(row, "cells", []):
                # Only include genuine origin cells for .docx table construction
                if not getattr(cell, "is_merge_origin", True):
                    continue

                # Extract text, icon_path, image_path, and background_color from cell content
                cell_text_parts: list[str] = []
                icon_path: Optional[str] = None
                image_path: Optional[str] = None
                # Real cell shading (w:shd) wins over paragraph-level highlighting
                background_color: Optional[str] = getattr(cell, "shading_hex", None)

                for item in getattr(cell, "content", []):
                    item_type = getattr(item, "node_type", None)
                    if item_type in ("paragraph", "heading"):
                        t = (getattr(item, "text", "") or "").strip()
                        if t:
                            cell_text_parts.append(t)
                        # Capture highlight color from paragraph-level background
                        if not background_color:
                            para_bg = getattr(item, "highlight_color", None) or getattr(item, "background_color", None)
                            if para_bg:
                                background_color = para_bg
                    elif item_type == "highlight":
                        # Standalone HighlightNode — carry its color as cell background
                        t = (getattr(item, "text", "") or "").strip()
                        if t:
                            cell_text_parts.append(t)
                        if not background_color:
                            background_color = getattr(item, "color_hex", None) or getattr(item, "highlight_color", None)
                    elif item_type == "icon":
                        ip = getattr(item, "asset_path", None)
                        if ip and not icon_path:
                            icon_path = ip
                    elif item_type == "image":
                        ip = getattr(item, "asset_path", None)
                        if ip and not image_path:
                            image_path = ip
                    elif item_type == "list":
                        for litem in getattr(item, "items", []):
                            lt = (getattr(litem, "text", "") or "").strip()
                            if lt:
                                cell_text_parts.append(f"- {lt}")

                text = " ".join(cell_text_parts).strip()
                text = clean_table_cell_text(text)

                # Small cell pictures in a legend table are icons, not figures.
                if image_path and not icon_path and not text:
                    icon_path = image_path
                    image_path = None

                cells.append(
                    MigrationTableCell(
                        row_index=getattr(cell, "row_index", row_idx),
                        col_index=getattr(cell, "col_index", 0),
                        row_span=getattr(cell, "row_span", 1),
                        col_span=getattr(cell, "col_span", 1),
                        text=text,
                        icon_path=icon_path,
                        image_path=image_path,
                        is_header=is_header,
                        background_color=background_color,
                    )
                )

        if not any(c.text or c.icon_path or c.image_path or c.background_color for c in cells):
            return None

        cells, num_rows, num_cols = cls._collapse_migration_cells(cells, num_rows, num_cols)

        return MigrationElement(
            element_type="table",
            page=page,
            title=getattr(table, "caption", None),
            num_rows=num_rows,
            num_cols=num_cols,
            cells=cells,
        )

    @classmethod
    def _collapse_migration_cells(
        cls,
        cells: list[MigrationTableCell],
        num_rows: int,
        num_cols: int,
    ) -> tuple[list[MigrationTableCell], int, int]:
        """Collapse sparse pdfplumber grids (empty columns, split description rows)."""
        if not cells or num_rows <= 0 or num_cols <= 0:
            return cells, num_rows, num_cols

        header_flags: dict[tuple[int, int], bool] = {}
        bg_flags: dict[tuple[int, int], Optional[str]] = {}
        grid: list[list[ExtractedTableCell]] = [
            [ExtractedTableCell(content_text="") for _ in range(num_cols)]
            for _ in range(num_rows)
        ]
        for cell in cells:
            r, c = cell.row_index, cell.col_index
            if r < 0 or c < 0 or r >= num_rows or c >= num_cols:
                continue
            media = []
            path = cell.icon_path or cell.image_path
            if path:
                media.append(ExtractedImage(image_path=path, page=0))
            grid[r][c] = ExtractedTableCell(
                content_text=cell.text or "",
                media_nodes=media,
                row_span=1,
                col_span=1,
                is_merge_origin=True,
            )
            header_flags[(r, c)] = cell.is_header
            bg_flags[(r, c)] = cell.background_color

        collapsed = collapse_sparse_grid(grid)
        if not collapsed:
            return cells, num_rows, num_cols

        new_rows = len(collapsed)
        new_cols = len(collapsed[0]) if collapsed else 0
        new_cells: list[MigrationTableCell] = []
        for r_idx, row in enumerate(collapsed):
            for c_idx, raw in enumerate(row):
                icon_path = None
                image_path = None
                for media in raw.media_nodes:
                    path = getattr(media, "image_path", None) or getattr(media, "asset_path", None)
                    et = getattr(media, "element_type", None)
                    if path and (et == "icon" or (not raw.content_text and not icon_path)):
                        icon_path = path
                    elif path and not image_path:
                        image_path = path
                if icon_path and image_path == icon_path:
                    image_path = None
                text = clean_table_cell_text(raw.content_text or "")
                if not text and not icon_path and not image_path:
                    continue
                is_header = header_flags.get((r_idx, c_idx), False) or (r_idx == 0 and any(
                    header_flags.get((0, c), False) for c in range(num_cols)
                ))
                new_cells.append(
                    MigrationTableCell(
                        row_index=r_idx,
                        col_index=c_idx,
                        row_span=1,
                        col_span=1,
                        text=text,
                        icon_path=icon_path,
                        image_path=image_path,
                        is_header=is_header if r_idx == 0 else False,
                        background_color=bg_flags.get((r_idx, c_idx)),
                    )
                )
        if not new_cells:
            return cells, num_rows, num_cols
        return new_cells, new_rows, new_cols

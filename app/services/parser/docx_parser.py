"""Concrete DOCX parser using python-docx.

Extracts paragraphs with style-based classification, tables,
embedded images, and document-level metadata from .docx files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from docx import Document as DocxDocument
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from app.config.settings import Settings
from app.services.extraction.metadata_extractor import SOPMetadataExtractor
from app.schemas.document import (
    BoundingBox,
    DocumentMetadata,
    ElementType,
    ExtractedElement,
    ExtractedHeading,
    ExtractedImage,
    ExtractedTable,
    PageContent,
    RawDocument,
)
from .base_parser import BaseParser
from .ooxml import NumberingInfo, NumberingResolver, element_text, iter_body_blocks


class DocxParser(BaseParser):
    """Parse .docx files into a structured RawDocument."""

    # Map python-docx style names → our ElementType & heading level.
    _HEADING_STYLES: dict[str, int] = {
        "Title": 1,
        "Heading 1": 1,
        "Heading 2": 2,
        "Heading 3": 3,
        "Heading 4": 4,
        "Heading 5": 5,
        "Heading 6": 6,
    }

    # Treat any paragraph with Word numbering (w:numPr) as a list item, not only
    # the list styles below. The template parser turns this off: its numbered
    # blue instructions are instructions, not content lists.
    _NUMPR_LISTS: bool = True

    _LIST_STYLES: set[str] = {
        "List Bullet",
        "List Bullet 2",
        "List Bullet 3",
        "List Number",
        "List Number 2",
        "List Number 3",
        "List Paragraph",
    }

    # ── Public interface ────────────────────────────────────────────

    def parse(self, file_path: str, document_id: str | None = None) -> RawDocument:
        """Open *file_path*, extract content, and return a RawDocument."""
        self._validate_file(file_path)
        self.logger.info(f"DocxParser: opening {file_path}")
        self.current_document_id = document_id

        doc = DocxDocument(file_path)

        metadata = self._extract_metadata(doc, file_path, document_id=document_id)
        elements: list[ExtractedElement] = []
        sequence = 0
        self._current_page = 1  # Track current page number while iterating

        # --- Iterate body in document order (paragraphs + tables interleaved) ---
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph
        from docx.oxml.ns import qn

        extracted_image_hashes: set[str] = set()  # track inline-extracted images to avoid duplicates
        self._extracted_hash_paths: dict[str, Path] = {}
        self._numbering = NumberingResolver(doc)
        self._paragraph_index = 0

        # Block content controls are flattened; the TOC control is skipped.
        for block in iter_body_blocks(doc.element.body):
            tag = block.tag
            if tag == qn('w:p'):
                para = Paragraph(block, doc)
                # Check for a page break BEFORE classifying so the element lands
                # on the correct (new) page.
                if self._paragraph_starts_new_page(para):
                    self._current_page += 1

                # Advance numbering for every paragraph, empty ones included,
                # as Word does.
                numbering = self._numbering.advance(block)
                paragraph_index = self._paragraph_index
                self._paragraph_index += 1
                element = self._classify_paragraph(
                    para, sequence,
                    numbering=numbering,
                    outline_level=self._numbering.outline_level(block),
                    paragraph_index=paragraph_index,
                )
                if element is not None:
                    elements.append(element)
                    sequence += 1

                # Extract inline images from this paragraph in document order
                inline_images = self._extract_inline_images(block, doc, sequence, extracted_image_hashes)
                for img_el in inline_images:
                    # Same index as the paragraph's text, so an inline icon stays with its own text.
                    img_el.metadata["paragraph_index"] = paragraph_index
                    elements.append(img_el)
                    sequence += 1

            elif tag == qn('w:tbl'):
                table = DocxTable(block, doc)
                table_element = self._extract_table(table, sequence, doc=doc, extracted_hashes=extracted_image_hashes)
                if table_element is not None:
                    elements.append(table_element)
                    sequence += 1

        # --- Images fallback (relationship-based, catches images not found inline) ---
        image_elements = self._extract_images(doc, sequence, skip_hashes=extracted_image_hashes)
        elements.extend(image_elements)

        # Group elements into per-page PageContent objects.
        pages = self._group_elements_into_pages(elements)

        self.logger.info(
            f"DocxParser: extracted {len(elements)} element(s) across "
            f"{len(pages)} page(s) from {file_path}"
        )

        return RawDocument(
            source=file_path,
            metadata=metadata,
            pages=pages,
        )

    # ── Metadata ────────────────────────────────────────────────────

    def _extract_metadata(
        self, doc: DocxDocument, file_path: str, document_id: str | None = None
    ) -> DocumentMetadata:
        """Pull document-level metadata from DOCX core properties and first-page table."""
        props = doc.core_properties
        file_stat = Path(file_path).stat()

        doc_title, doc_name, doc_num, doc_ver, doc_type = SOPMetadataExtractor.extract_from_file(
            file_path, fallback_filename=Path(file_path).name
        )
        gpdat_version = self._next_gpdat_version(document_id)

        return DocumentMetadata(
            title=doc_title or props.title or "",
            author=props.author or "",
            subject=props.subject or "",
            creator=props.last_modified_by or "",
            creation_date=(
                props.created.isoformat() if props.created else ""
            ),
            modification_date=(
                props.modified.isoformat() if props.modified else ""
            ),
            page_count=0,  # Not available at parse time in python-docx
            file_type="docx",
            file_size_bytes=file_stat.st_size,
            document_title=doc_title,
            document_name=doc_name,
            document_number=doc_num,
            document_version=doc_ver,
            document_type=doc_type,
            gpdat_version=gpdat_version,
        )

    # ── Page-break detection ─────────────────────────────────────────

    def _paragraph_starts_new_page(self, para) -> bool:
        """Return True if *para* triggers a new page in the rendered document.

        python-docx doesn't expose page breaks through its high-level API, so
        we inspect the raw OOXML directly.  We check three sources (in order of
        reliability):

        1. ``<w:lastRenderedPageBreak/>`` — inserted by Word/LibreOffice when
           the document is saved after a full render.  Marks *layout-driven*
           overflow breaks as well as explicit ones.  Most accurate when present.
        2. Explicit run break: ``<w:br w:type="page"/>`` or ``<w:br w:type="column"/>``
        3. Section break: ``<w:sectPr>`` whose ``<w:type>`` is NOT "continuous"
           (nextPage / evenPage / oddPage all start a new page).
        """
        from docx.oxml.ns import qn

        pPr = para._element.find(qn('w:pPr'))

        # ── 1. Layout-driven breaks: <w:lastRenderedPageBreak/> ──────────
        # Word / LibreOffice embed this tag inside runs when saving a rendered
        # document.  It covers both explicit and overflow page breaks, making
        # it the most complete signal when available.
        for _ in para._element.iter(qn('w:lastRenderedPageBreak')):
            return True

        # ── 2. Explicit <w:br w:type="page"/> in any run ─────────────────
        for br in para._element.iter(qn('w:br')):
            br_type = br.get(qn('w:type'), '')
            if br_type in ('page', 'column'):
                return True

        # ── 3. Section break via <w:sectPr> in paragraph props ───────────
        if pPr is not None:
            sectPr = pPr.find(qn('w:sectPr'))
            if sectPr is not None:
                type_el = sectPr.find(qn('w:type'))
                sect_type = type_el.get(qn('w:val'), 'nextPage') if type_el is not None else 'nextPage'
                # 'continuous' does NOT start a new page; everything else does
                if sect_type != 'continuous':
                    return True

        return False

    def _group_elements_into_pages(self, elements: list[ExtractedElement]) -> list[PageContent]:
        """Group a flat list of elements into PageContent objects keyed by page number."""
        from collections import defaultdict
        page_map: dict[int, list[ExtractedElement]] = defaultdict(list)
        for el in elements:
            page_map[el.page].append(el)

        if not page_map:
            # Return a single empty page so downstream code always gets at least one page.
            return [PageContent(page_number=1, elements=[])]

        return [
            PageContent(page_number=pnum, elements=page_map[pnum])
            for pnum in sorted(page_map)
        ]

    # ── Shading / formatting helpers ─────────────────────────────────

    @staticmethod
    def _shading_fill(props_el) -> Optional[str]:
        """Return the ``w:shd`` fill hex from a ``tcPr``/``pPr``, or None when unset.

        Word writes ``w:fill="auto"`` for "no fill", which is not a colour.
        """
        from docx.oxml.ns import qn

        if props_el is None:
            return None
        shd = props_el.find(qn('w:shd'))
        if shd is None:
            return None
        fill = (shd.get(qn('w:fill')) or "").strip().upper().lstrip("#")
        if len(fill) != 6 or fill in {"AUTO", "NIL", "NONE"}:
            return None
        return fill

    # ── Paragraph classification ────────────────────────────────────

    def _classify_paragraph(
        self, para, sequence: int,
        numbering: Optional[NumberingInfo] = None,
        outline_level: Optional[int] = None,
        paragraph_index: Optional[int] = None,
    ) -> Optional[ExtractedElement]:
        """Classify a python-docx paragraph into a typed ExtractedElement.

        *numbering* and *outline_level* come from ``NumberingResolver``; when
        given, a heading gets its Word number ("6.1.2") and a numbered paragraph
        becomes a list item with its level.
        """
        text = para.text.strip()
        if not text:
            return None

        style_name = para.style.name if para.style else "Normal"

        # Check for watermark keywords or styles
        keywords = getattr(self.settings, "watermark_keywords", []) if hasattr(self, "settings") else getattr(self.config, "watermark_keywords", [])
        if not keywords:
            keywords = ["WORKING COPY", "DRAFT", "CONFIDENTIAL", "DO NOT DISTRIBUTE", "WATERMARK"]
        
        text_upper = text.upper()
        style_upper = style_name.upper()
        
        # If the style has 'WATERMARK' in the name, or if the text matches a keyword
        if "WATERMARK" in style_upper or any(kw.upper() in text_upper for kw in keywords):
            self.logger.debug(f"DocxParser: Ignored watermark: {text[:30]}")
            return None

        current_page = getattr(self, '_current_page', 1)

        heading_level = self._HEADING_STYLES.get(style_name)
        if heading_level is None and outline_level is not None and style_name not in self._LIST_STYLES:
            heading_level = outline_level + 1

        element: ExtractedElement
        if heading_level is not None:
            element = ExtractedHeading(
                content=text,
                page=current_page,
                sequence=sequence,
                level=heading_level,
                style_name=style_name,
            )
            if numbering is not None and numbering.number:
                element.metadata["numbering"] = numbering.number
                element.metadata["numbering_source"] = "numPr"
        elif style_name in self._LIST_STYLES or (self._NUMPR_LISTS and numbering is not None):
            # Distinguish numbered lists from bullet lists
            element_type = ElementType.LIST_ITEM
            if "Number" in style_name or (numbering is not None and numbering.number):
                element_type = ElementType.NUMBERED_STEP
            element = ExtractedElement(
                element_type=element_type,
                content=text,
                page=current_page,
                sequence=sequence,
            )
            if numbering is not None:
                element.metadata["list_level"] = numbering.ilvl
                element.metadata["num_id"] = numbering.num_id
                if numbering.number:
                    element.metadata["list_number"] = numbering.number
        else:
            element = ExtractedElement(
                element_type=ElementType.PARAGRAPH,
                content=text,
                page=current_page,
                sequence=sequence,
            )

        from docx.oxml.ns import qn
        element.shading_hex = self._shading_fill(para._element.find(qn('w:pPr')))
        if outline_level is not None:
            element.outline_level = outline_level
        if paragraph_index is not None:
            element.metadata["paragraph_index"] = paragraph_index
        return element

    # ── Table extraction ────────────────────────────────────────────

    def _extract_table(
        self, table, sequence: int, doc: Optional[DocxDocument] = None,
        extracted_hashes: set[str] | None = None,
    ) -> Optional[ExtractedTable]:
        """Convert a python-docx table into an ExtractedTable with merged cell detection and cell media extraction."""
        from docx.oxml.ns import qn
        from app.schemas.document import ExtractedTableCell
        
        rows_data: list[list[ExtractedTableCell]] = []

        # ``row.cells`` is grid-aligned: a merged cell is repeated once per grid
        # position it covers, and a vertically merged position returns a wrapper
        # around the *origin* row's ``<w:tc>``. Reading gridSpan/vMerge off those
        # wrappers would therefore duplicate content, so the merge structure is
        # taken from the row's own ``tc_lst`` and walked in lockstep.
        last_origin_row_at_col: dict[int, int] = {}
        from docx.table import _Cell

        grid_el = table._tbl.find(qn('w:tblGrid'))
        grid_width = len(grid_el.findall(qn('w:gridCol'))) if grid_el is not None else 0

        for row_idx, row in enumerate(table.rows):
            cell_row: list[ExtractedTableCell] = []
            grid_pos = 0

            # Cells can sit inside a row-level content control (w:sdt), e.g. cover-sheet
            # fields; python-docx's row.cells and tc_lst skip those.
            for tc in self._row_cells(row._tr):
                tcPr = tc.find(qn('w:tcPr'))

                col_span = 1
                if tcPr is not None:
                    gridSpan = tcPr.find(qn('w:gridSpan'))
                    if gridSpan is not None:
                        col_span = int(gridSpan.get(qn('w:val'), '1'))

                vMerge = tcPr.find(qn('w:vMerge')) if tcPr is not None else None
                is_vertical_continuation = (
                    vMerge is not None
                    and vMerge.get(qn('w:val'), 'continue') != 'restart'
                )

                for span_idx in range(col_span):
                    if grid_width and grid_pos >= grid_width:
                        break
                    cell = _Cell(tc, table)

                    if is_vertical_continuation:
                        # Extend the origin's row_span once per covered row.
                        origin_row = last_origin_row_at_col.get(grid_pos)
                        if span_idx == 0 and origin_row is not None:
                            rows_data[origin_row][grid_pos].row_span += 1
                        cell_row.append(
                            ExtractedTableCell(
                                content_text="",
                                is_merge_origin=False,
                                merge_origin_ref="vertical",
                            )
                        )
                        grid_pos += 1
                        continue

                    if span_idx > 0:
                        cell_row.append(
                            ExtractedTableCell(
                                content_text="",
                                is_merge_origin=False,
                                merge_origin_ref="horizontal",
                            )
                        )
                        grid_pos += 1
                        continue

                    # Extract inline images from table cell
                    cell_media = []
                    if doc is not None and extracted_hashes is not None:
                        cell_media = self._extract_inline_images(tc, doc, sequence, extracted_hashes)
                        sequence += len(cell_media)

                    text_direction = None
                    valign = None
                    if tcPr is not None:
                        td = tcPr.find(qn('w:textDirection'))
                        if td is not None:
                            text_direction = td.get(qn('w:val')) or None
                        va = tcPr.find(qn('w:vAlign'))
                        if va is not None:
                            valign = va.get(qn('w:val')) or None

                    cell_metadata: dict = {}
                    left_border = self._cell_left_border_color(tcPr)
                    if left_border:
                        cell_metadata["border_left_color_hex"] = left_border

                    resolver = getattr(self, "_numbering", None)
                    if resolver is not None:
                        for cell_p in tc.iter(qn('w:p')):
                            resolver.advance(cell_p)

                    cell_row.append(
                        ExtractedTableCell(
                            # Includes text inside inline content controls, which cell.text drops.
                            content_text=element_text(tc).strip(),
                            col_span=col_span,
                            row_span=1,  # extended as vMerge continuations are seen
                            is_merge_origin=True,
                            media_nodes=cell_media,
                            shading_hex=self._shading_fill(tcPr),
                            text_direction=text_direction,
                            valign=valign,
                            bold=any(
                                run.bold for para in cell.paragraphs for run in para.runs
                            ),
                            metadata=cell_metadata,
                        )
                    )
                    last_origin_row_at_col[grid_pos] = row_idx
                    grid_pos += 1

            rows_data.append(cell_row)

        if not rows_data:
            return None

        # One entry per grid position, so the widest row is the grid width.
        max_cols = max(len(row) for row in rows_data)

        headers = rows_data[0] if rows_data else []
        data_rows = rows_data[1:] if len(rows_data) > 1 else []

        return ExtractedTable(
            content=f"[Table: {len(headers)} cols × {len(data_rows)} rows]",
            page=getattr(self, '_current_page', 1),
            sequence=sequence,
            grid_cols=max_cols,
            headers=headers,
            rows=data_rows,
            style_name=self._table_style_name(table),
            col_widths_pt=self._table_col_widths_pt(table),
            header_rows=self._table_header_row_count(table),
        )

    @staticmethod
    def _row_cells(tr) -> list:
        """The row's ``w:tc`` elements in order, including cells inside row-level ``w:sdt``."""
        from docx.oxml.ns import qn

        cells = []
        for child in tr:
            if child.tag == qn('w:tc'):
                cells.append(child)
            elif child.tag == qn('w:sdt'):
                content = child.find(qn('w:sdtContent'))
                if content is not None:
                    cells.extend(DocxParser._row_cells(content))
        return cells

    @staticmethod
    def _cell_left_border_color(tcPr) -> Optional[str]:
        """Return the cell's left border colour hex — the accent bar on callouts."""
        from docx.oxml.ns import qn

        if tcPr is None:
            return None
        borders = tcPr.find(qn('w:tcBorders'))
        if borders is None:
            return None
        left = borders.find(qn('w:left'))
        if left is None:
            return None
        color = (left.get(qn('w:color')) or "").strip().upper().lstrip("#")
        if len(color) != 6 or color == "AUTO":
            return None
        return color

    @staticmethod
    def _table_style_name(table) -> Optional[str]:
        """Return the table's ``w:tblStyle`` value, or None."""
        from docx.oxml.ns import qn

        tblPr = table._tbl.find(qn('w:tblPr'))
        if tblPr is None:
            return None
        style = tblPr.find(qn('w:tblStyle'))
        if style is None:
            return None
        return style.get(qn('w:val')) or None

    @staticmethod
    def _table_col_widths_pt(table) -> list[float]:
        """Return the declared column widths in points (``w:tblGrid`` is in twips)."""
        from docx.oxml.ns import qn

        grid = table._tbl.find(qn('w:tblGrid'))
        if grid is None:
            return []
        widths: list[float] = []
        for col in grid.findall(qn('w:gridCol')):
            raw = col.get(qn('w:w'))
            if raw is None:
                continue
            try:
                widths.append(round(int(raw) / 20.0, 1))
            except ValueError:
                continue
        return widths

    @staticmethod
    def _table_header_row_count(table) -> int:
        """Count leading rows flagged as repeating headers (``w:tblHeader``)."""
        from docx.oxml.ns import qn

        count = 0
        for row in table.rows:
            trPr = row._tr.find(qn('w:trPr'))
            if trPr is not None and trPr.find(qn('w:tblHeader')) is not None:
                count += 1
            else:
                break
        return count

    # ── Image extraction ────────────────────────────────────────────

    def _extract_inline_images(
        self, paragraph_element, doc: DocxDocument, start_sequence: int,
        extracted_hashes: set[str],
    ) -> list[ExtractedImage]:
        """Extract images embedded inline within a paragraph or cell XML element.

        Looks for ``<w:drawing>`` and ``<w:pict>`` elements containing image
        relationship references (``r:embed`` or ``r:id``), and resolves them
        to actual image data via the document's relationship table.
        """
        from docx.oxml.ns import qn

        images: list[ExtractedImage] = []
        sequence = start_sequence

        # Namespace for relationships
        r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

        # Collect all rId references from inline drawings/pictures
        rids: list[str] = []
        # Displayed size per rId, from the drawing's wp:extent (EMU; 9525 EMU = 1 px at 96 dpi).
        sizes_px: dict[str, tuple[int, int]] = {}
        wp_extent = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent'
        for drawing in paragraph_element.iter(qn('w:drawing')):
            extent = next(drawing.iter(wp_extent), None)
            if extent is None:
                continue
            try:
                size = (int(extent.get('cx')) // 9525, int(extent.get('cy')) // 9525)
            except (TypeError, ValueError):
                continue
            for blip in drawing.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}blip'):
                embed = blip.get(f'{{{r_ns}}}embed')
                if embed:
                    sizes_px.setdefault(embed, size)

        # <w:drawing> -> <wp:inline> or <wp:anchor> -> <a:graphic> -> <a:graphicData> -> <pic:pic> -> <pic:blipFill> -> <a:blip r:embed="rId...">
        for blip in paragraph_element.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}blip'):
            embed = blip.get(f'{{{r_ns}}}embed')
            if embed:
                rids.append(embed)
            # Register svgBlip partners so vector duplicates are skipped by fallback
            for s in blip.xpath('.//*[local-name()="svgBlip"]'):
                s_embed = s.get(f'{{{r_ns}}}embed')
                if s_embed and s_embed in doc.part.rels:
                    try:
                        s_bytes = doc.part.rels[s_embed].target_part.blob
                        extracted_hashes.add(hashlib.md5(s_bytes).hexdigest()[:10])
                    except Exception:
                        pass

        # <w:pict> -> <v:shape> -> <v:imagedata r:id="rId...">
        for imagedata in paragraph_element.iter('{urn:schemas-microsoft-com:vml}imagedata'):
            rid = imagedata.get(f'{{{r_ns}}}id')
            if rid:
                rids.append(rid)

        for rid in rids:
            try:
                rel = doc.part.rels.get(rid)
                if rel is None or "image" not in rel.reltype:
                    continue

                image_part = rel.target_part
                image_bytes = image_part.blob
                img_hash = hashlib.md5(image_bytes).hexdigest()[:10]

                # If already extracted, reuse the saved path so subsequent occurrences (e.g. repeated icons) retain the image
                if hasattr(self, "_extracted_hash_paths") and img_hash in self._extracted_hash_paths:
                    save_path = self._extracted_hash_paths[img_hash]
                    images.append(
                        ExtractedImage(
                            content=f"[Image: {save_path.name}]",
                            page=getattr(self, '_current_page', 1),
                            sequence=sequence,
                            image_path=str(save_path),
                            content_hash=img_hash,
                            width=sizes_px.get(rid, (0, 0))[0],
                            height=sizes_px.get(rid, (0, 0))[1],
                        )
                    )
                    sequence += 1
                    continue

                extracted_hashes.add(img_hash)

                content_type = image_part.content_type or "image/png"
                ext = content_type.split("/")[-1]
                if ext == "jpeg":
                    ext = "jpg"

                filename = f"docx_img{sequence}_{img_hash}.{ext}"
                doc_id = getattr(self, "current_document_id", None) or "default"
                save_path = self.settings.get_document_image_dir(doc_id) / filename

                save_path.write_bytes(image_bytes)
                if hasattr(self, "_extracted_hash_paths"):
                    self._extracted_hash_paths[img_hash] = save_path

                images.append(
                    ExtractedImage(
                        content=f"[Image: {filename}]",
                        page=getattr(self, '_current_page', 1),
                        sequence=sequence,
                        image_path=str(save_path),
                        content_hash=img_hash,
                        width=sizes_px.get(rid, (0, 0))[0],
                        height=sizes_px.get(rid, (0, 0))[1],
                    )
                )
                sequence += 1

            except Exception:
                self.logger.warning(
                    f"DocxParser: failed to extract inline image from rId {rid}"
                )
                continue

        return images

    def _extract_images(
        self, doc: DocxDocument, start_sequence: int,
        skip_hashes: set[str] | None = None,
    ) -> list[ExtractedImage]:
        """Extract embedded images from the DOCX media folder (fallback).

        Images already extracted inline (tracked by *skip_hashes*) are skipped.
        """
        images: list[ExtractedImage] = []
        sequence = start_sequence

        for rel in doc.part.rels.values():
            if "image" not in rel.reltype:
                continue

            try:
                image_part = rel.target_part
                image_bytes = image_part.blob
                content_type = image_part.content_type or "image/png"
                ext = content_type.split("/")[-1]
                if ext == "jpeg":
                    ext = "jpg"

                img_hash = hashlib.md5(image_bytes).hexdigest()[:10]

                # Skip images already extracted inline
                if skip_hashes and img_hash in skip_hashes:
                    continue

                filename = f"docx_img{sequence}_{img_hash}.{ext}"
                doc_id = getattr(self, "current_document_id", None) or "default"
                save_path = self.settings.get_document_image_dir(doc_id) / filename

                save_path.write_bytes(image_bytes)

                images.append(
                    ExtractedImage(
                        content=f"[Image: {filename}]",
                        page=getattr(self, '_current_page', 1),
                        sequence=sequence,
                        image_path=str(save_path),
                        content_hash=img_hash,
                    )
                )
                sequence += 1

            except Exception:
                self.logger.warning(
                    f"DocxParser: failed to extract image from relationship "
                    f"{rel.rId}"
                )
                continue

        return images


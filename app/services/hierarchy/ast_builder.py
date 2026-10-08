"""AST Builder — constructs a typed document tree from extracted elements.

Replaces and extends the v1 ``TreeBuilder`` by producing a fully-typed AST
of ``ASTNode`` subclasses instead of the simpler ``SectionNode`` objects.

The builder:
1. Accepts a ``RawDocument`` (already reordered by ``PDFLayoutAnalyzer``
   for PDFs).
2. Walks elements in reading order, maintaining a section stack.
3. Produces the correct hierarchy: headings → sections, paragraphs,
   lists, tables, images, icons, captions.
4. Returns a ``DocumentNode`` (the AST root).

**NOTE**: This builder handles the *structural assembly* only.  Rich
extraction (list grouping, highlight detection, merged cells, OCR) is
performed by specialized extractors *before* the builder runs.  The builder
consumes their enriched output.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger as _default_logger

from app.config.settings import Settings
from app.schemas.document import (
    DocumentMetadata,
    ElementType,
    ExtractedElement,
    ExtractedHeading,
    ExtractedImage,
    ExtractedIcon,
    ExtractedTable,
    PageContent,
    RawDocument,
)
from app.schemas.ast_nodes import (
    ASTNode,
    CaptionNode,
    DocumentNode,
    HeadingNode,
    IconNode,
    ImageNode,
    ListItemNode,
    ListNode,
    ParagraphNode,
    SectionNode,
    SourceLocation,
    TableCellNode,
    TableNode,
    TableRowNode,
)


class ASTBuilder:
    """Build a typed AST from a parsed ``RawDocument``.

    Usage::

        builder = ASTBuilder(settings)
        ast_root = builder.build(raw_document)
    """

    # Pattern for detecting numbered/bulleted list items
    _LIST_BULLET_RE = re.compile(
        r"^[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25AB\u25CF\u25CB\u25A0\u25A1\u2013\u2014○●◆◇■□▪▫–—•‣⁃]\s*"
    )
    _LIST_ORDERED_RE = re.compile(
        r"^(?:\d{1,3}[.)]\s|[a-zA-Z][.)]\s|[ivxlcdm]+[.)]\s)", re.IGNORECASE
    )

    def __init__(self, settings: Settings, logger=None) -> None:
        self.settings = settings
        self.logger = logger or _default_logger

    # ── Public API ────────────────────────────────────────────────────

    def build(self, document: RawDocument) -> DocumentNode:
        """Build the complete AST from *document*.

        Returns the root ``DocumentNode`` with all children assembled.
        """
        self.logger.info(f"ASTBuilder: building AST for {document.source}")

        root = DocumentNode(
            node_id=str(uuid.uuid4()),
            doc_metadata=document.metadata,
        )

        # Flatten all page elements in reading order
        all_elements = self._flatten_elements(document)

        # Section stack for heading-based hierarchy
        # The root acts as a virtual level-0 section
        root_section = SectionNode(
            node_id=root.node_id,
            level=0,
            heading=HeadingNode(
                text=document.metadata.title or "Document Root",
                level=0,
            ),
        )
        stack: list[SectionNode] = [root_section]

        sequence = 0
        i = 0
        while i < len(all_elements):
            element = all_elements[i]

            if element.element_type == ElementType.HEADING and not self._is_heading_actually_list(element):
                heading_el = element  # type: ExtractedHeading
                heading_node = self._make_heading_node(heading_el, sequence)

                section = SectionNode(
                    node_id=str(uuid.uuid4()),
                    heading=heading_node,
                    level=heading_el.level,
                    sequence=sequence,
                    source_location=self._make_source_loc(heading_el),
                )

                # Protect major section: if stack has a major section (e.g. "6 PRINCIPLES FOR DOCUMENT WRITING"),
                # do not allow an arbitrary heading to pop back to root unless the incoming heading is also a genuine major section heading
                content_clean = heading_el.content.strip()
                is_incoming_major = False
                if heading_el.level == 1:
                    if not re.match(r"^\d+\.\d+", content_clean) and not content_clean.endswith(":"):
                        m_ref = re.match(r"^(\d+)\s+([A-Z0-9\-_]+)", content_clean)
                        if m_ref and (m_ref.group(2) in ("N/A", "NONE", "NO.") or m_ref.group(2).startswith("BI-")):
                            is_incoming_major = False
                        else:
                            is_incoming_major = True

                min_stack_len = 1 if is_incoming_major else (2 if len(stack) > 1 else 1)

                # Pop stack until we find a parent with a strictly lower level
                while len(stack) > min_stack_len and stack[-1].level >= heading_el.level:
                    stack.pop()

                parent = stack[-1]
                parent.children.append(section)
                stack.append(section)
                sequence += 1

            elif element.element_type == ElementType.TABLE:
                table_el = element  # type: ExtractedTable
                table_node = self._make_table_node(table_el, sequence)
                current = stack[-1]
                current.children.append(table_node)
                sequence += 1

            elif element.element_type == ElementType.IMAGE:
                image_el = element  # type: ExtractedImage
                image_node = self._make_image_node(image_el, sequence)
                current = stack[-1]
                current.children.append(image_node)
                sequence += 1

            elif element.element_type == ElementType.ICON:
                icon_el = element  # type: ExtractedIcon
                icon_node = self._make_icon_node(icon_el, sequence)
                current = stack[-1]
                current.children.append(icon_node)
                sequence += 1

            elif element.element_type == ElementType.CAPTION:
                caption_node = self._make_caption_node(element, sequence)
                current = stack[-1]
                # Try to attach to the last child (image or table)
                if current.children:
                    last_child = current.children[-1]
                    if isinstance(last_child, (ImageNode, TableNode)):
                        caption_node.referenced_node_id = last_child.node_id
                        if isinstance(last_child, ImageNode):
                            last_child.caption = element.content
                        elif isinstance(last_child, TableNode):
                            last_child.caption = element.content
                current.children.append(caption_node)
                sequence += 1

            elif self._is_list_item(element):
                # Consume consecutive list items into a ListNode
                list_node, consumed = self._consume_list_items(
                    all_elements, i, sequence,
                )
                current = stack[-1]
                current.children.append(list_node)
                sequence += consumed
                i += consumed
                continue  # skip the i += 1 at the end

            else:
                # Default: paragraph
                # Check if it's a continuation of the previous paragraph or list item
                is_continuation = False
                text_content = element.content.strip()
                current = stack[-1]
                
                if text_content and current.children:
                    first_char = text_content[0]
                    if first_char.islower() or first_char in ',);]-':
                        last_child = current.children[-1]
                        if isinstance(last_child, ParagraphNode):
                            last_child.text += " " + text_content
                            is_continuation = True
                        elif isinstance(last_child, ListNode) and last_child.items:
                            last_item = last_child.items[-1]
                            last_item.text += " " + text_content
                            is_continuation = True
                            
                if not is_continuation:
                    para_node = self._make_paragraph_node(element, sequence)
                    current.children.append(para_node)
                    sequence += 1

            i += 1

        # Transfer root_section children to the DocumentNode
        root.children = root_section.children
        return root

    # ── Element → Node Converters ─────────────────────────────────────

    def _make_heading_node(
        self, el: ExtractedHeading, seq: int,
    ) -> HeadingNode:
        """Convert an ``ExtractedHeading`` to a ``HeadingNode``."""
        # Word auto-numbering (computed by the DOCX parser) wins over a number typed in the text.
        numbering = (getattr(el, "metadata", None) or {}).get("numbering")
        if numbering is None:
            match = re.match(r"^(\d+(?:\.\d+)*\.?)\s", el.content.strip())
            if match:
                numbering = match.group(1).rstrip(".")

        return HeadingNode(
            node_id=str(uuid.uuid4()),
            text=el.content.strip(),
            level=el.level,
            numbering=numbering,
            sequence=seq,
            source_location=self._make_source_loc(el),
            confidence=el.confidence,
            metadata=dict(getattr(el, "metadata", {}) or {}),
            style_name=getattr(el, "style_name", None),
            font_color_hex=getattr(el, "font_color_hex", None),
            color_detection_method=getattr(el, "color_detection_method", None),
            shading_hex=getattr(el, "shading_hex", None),
        )

    def _make_paragraph_node(
        self, el: ExtractedElement, seq: int,
    ) -> ParagraphNode:
        """Convert a generic paragraph element to a ``ParagraphNode``."""
        return ParagraphNode(
            node_id=str(uuid.uuid4()),
            text=el.content.strip(),
            sequence=seq,
            source_location=self._make_source_loc(el),
            confidence=el.confidence,
            metadata=dict(getattr(el, "metadata", {}) or {}),
            font_color_hex=getattr(el, "font_color_hex", None),
            color_detection_method=getattr(el, "color_detection_method", None),
            shading_hex=getattr(el, "shading_hex", None),
        )

    def _make_table_node(
        self, el: ExtractedTable, seq: int,
    ) -> TableNode:
        """Convert an ``ExtractedTable`` to a ``TableNode`` with rows/cells."""
        rows: list[TableRowNode] = []

        def _process_cell(raw_cell, r_idx, c_idx):
            cell_meta = dict(getattr(raw_cell, "metadata", {}) or {})
            cell_node = TableCellNode(
                node_id=str(uuid.uuid4()),
                row_index=r_idx,
                col_index=c_idx,
                row_span=raw_cell.row_span,
                col_span=raw_cell.col_span,
                is_merge_origin=raw_cell.is_merge_origin,
                merge_origin_ref=raw_cell.merge_origin_ref,
                metadata=cell_meta,
                content=[],
                shading_hex=getattr(raw_cell, "shading_hex", None),
                text_direction=getattr(raw_cell, "text_direction", None),
                valign=getattr(raw_cell, "valign", None),
                bold=bool(getattr(raw_cell, "bold", False)),
            )

            if raw_cell.content_text:
                cell_node.content.append(ParagraphNode(
                    node_id=str(uuid.uuid4()),
                    text=raw_cell.content_text,
                    metadata=cell_meta,
                    font_color_hex=cell_meta.get("font_color_hex"),
                    color_detection_method=cell_meta.get("color_detection_method"),
                    shading_hex=getattr(raw_cell, "shading_hex", None),
                ))
                
            for media in raw_cell.media_nodes:
                if media.element_type == ElementType.IMAGE:
                    cell_node.content.append(self._make_image_node(media, 0))
                elif media.element_type == ElementType.ICON:
                    cell_node.content.append(self._make_icon_node(media, 0))
                    
            return cell_node

        # Header row
        if el.headers:
            header_cells = []
            for col_idx, header_cell in enumerate(el.headers):
                header_cells.append(_process_cell(header_cell, 0, col_idx))

            rows.append(TableRowNode(
                node_id=str(uuid.uuid4()),
                row_index=0,
                is_header=True,
                cells=header_cells,
            ))

        # Data rows
        for row_idx, row_data in enumerate(el.rows):
            data_row_index = row_idx + (1 if el.headers else 0)
            cells = []
            for col_idx, cell_data in enumerate(row_data):
                cells.append(_process_cell(cell_data, data_row_index, col_idx))

            rows.append(TableRowNode(
                node_id=str(uuid.uuid4()),
                row_index=data_row_index,
                cells=cells,
            ))

        return TableNode(
            node_id=str(uuid.uuid4()),
            row_count=len(rows),
            col_count=len(el.headers) if el.headers else (
                len(el.rows[0]) if el.rows else 0
            ),
            grid_cols=el.grid_cols or (len(el.headers) if el.headers else (len(el.rows[0]) if el.rows else 0)),
            rows=rows,
            has_header_row=bool(el.headers),
            sequence=seq,
            source_location=self._make_source_loc(el),
            confidence=el.confidence,
            style_name=getattr(el, "style_name", None),
            col_widths_pt=list(getattr(el, "col_widths_pt", []) or []),
            header_rows=int(getattr(el, "header_rows", 0) or 0),
        )

    def _make_image_node(
        self, el: ExtractedImage, seq: int,
    ) -> ImageNode:
        """Convert an ``ExtractedImage`` to an ``ImageNode``."""
        return ImageNode(
            node_id=str(uuid.uuid4()),
            asset_path=el.image_path,
            alt_text=el.caption or "",
            width=el.width,
            height=el.height,
            caption=el.caption or None,
            sequence=seq,
            source_location=self._make_source_loc(el),
            confidence=el.confidence,
            image_hash=getattr(el, "content_hash", None) or "",
        )

    def _make_icon_node(
        self, el: ExtractedIcon, seq: int,
    ) -> IconNode:
        """Convert an ``ExtractedIcon`` to an ``IconNode``."""
        return IconNode(
            node_id=str(uuid.uuid4()),
            asset_path=el.image_path,
            semantic_meaning=el.semantic_meaning,
            sequence=seq,
            source_location=self._make_source_loc(el),
            confidence=el.confidence,
            content_hash=getattr(el, "content_hash", None) or self._hash_from_asset_name(el.image_path),
            metadata=dict(getattr(el, "metadata", None) or {}),
        )

    @staticmethod
    def _hash_from_asset_name(asset_path: str) -> Optional[str]:
        """Recover the content hash embedded in extracted asset filenames.

        Parsers name assets ``<prefix><seq>_<md5[:10]>.<ext>``, so the hash
        survives even when the icon reaches the AST without it set.
        """
        if not asset_path:
            return None
        stem = Path(asset_path).stem
        candidate = stem.rsplit("_", 1)[-1]
        if len(candidate) == 10 and all(c in "0123456789abcdef" for c in candidate.lower()):
            return candidate.lower()
        return None

    def _make_caption_node(
        self, el: ExtractedElement, seq: int,
    ) -> CaptionNode:
        """Convert a caption element to a ``CaptionNode``."""
        # Try to extract the label (e.g., "Figure 1", "Table 2")
        label = None
        label_match = re.match(
            r"^((?:Figure|Fig\.?|Table|Tbl\.?|Image|Diagram|Chart|Graph|Exhibit)\s*\d+)",
            el.content.strip(),
            re.IGNORECASE,
        )
        if label_match:
            label = label_match.group(1)

        return CaptionNode(
            node_id=str(uuid.uuid4()),
            text=el.content.strip(),
            caption_label=label,
            sequence=seq,
            source_location=self._make_source_loc(el),
            confidence=el.confidence,
        )

    # ── List Detection & Consumption ──────────────────────────────────

    def _is_heading_actually_list(self, el: ExtractedElement) -> bool:
        """Detect if an ExtractedHeading should be treated as a list item instead."""
        text = el.content.replace("\u200b", "").strip()
        if not text:
            return False
        # Any bullet character is always a list item, never a heading
        if self._LIST_BULLET_RE.match(text):
            return True
        # Numbered items that end with colon (e.g. "1. Active Voice is Key:") are list items/labels
        if self._LIST_ORDERED_RE.match(text) and text.endswith(":"):
            return True
        return False

    def _is_list_item(self, el: ExtractedElement) -> bool:
        """Check if an element looks like a list item."""
        if el.element_type == ElementType.LIST_ITEM:
            return True
        if el.element_type == ElementType.NUMBERED_STEP:
            return True
        text = el.content.replace("\u200b", "").strip()
        if self._LIST_BULLET_RE.match(text):
            return True
        if self._LIST_ORDERED_RE.match(text):
            return True
        return False

    def _detect_list_type(self, text: str) -> str:
        """Determine whether a list item text is ordered or unordered."""
        stripped = text.strip()
        if self._LIST_ORDERED_RE.match(stripped):
            return "ordered"
        return "unordered"

    def _consume_list_items(
        self,
        elements: list[ExtractedElement],
        start_index: int,
        start_seq: int,
    ) -> tuple[ListNode, int]:
        """Consume consecutive list items starting at *start_index*.

        Returns (ListNode, number_of_elements_consumed).
        """
        items: list[ListItemNode] = []
        i = start_index
        seq = start_seq

        # Determine list type from the first item: Word numbering when the
        # parser resolved it, otherwise the text prefix.
        first = elements[i]
        first_meta = getattr(first, "metadata", None) or {}
        if "list_level" in first_meta:
            list_type = "ordered" if first_meta.get("list_number") else "unordered"
        else:
            list_type = self._detect_list_type(first.content.strip())
        max_level = 0

        item_index = 1
        while i < len(elements):
            el = elements[i]
            
            if self._is_list_item(el):
                # Clean bullet/number prefix
                text = el.content.strip()
                clean_text = self._strip_list_prefix(text)
    
                item = ListItemNode(
                    node_id=str(uuid.uuid4()),
                    text=clean_text,
                    index=item_index if list_type == "ordered" else None,
                    sequence=seq,
                    source_location=self._make_source_loc(el),
                    confidence=el.confidence,
                    metadata=dict(getattr(el, "metadata", {}) or {}),
                )
                items.append(item)
                max_level = max(max_level, int(item.metadata.get("list_level") or 0))
                item_index += 1
                seq += 1
                i += 1
            else:
                # Check if it's a continuation paragraph
                text_content = el.content.strip()
                if text_content and items:
                    first_char = text_content[0]
                    if first_char.islower() or first_char in ',);]-':
                        items[-1].text += " " + text_content
                        # We don't increment seq here since it's merged
                        i += 1
                        continue
                # Not a list item and not a continuation -> break
                break

        list_node = ListNode(
            node_id=str(uuid.uuid4()),
            list_type=list_type,
            # Items stay flat (each keeps metadata["list_level"]); depth is the deepest level.
            nesting_depth=max_level,
            items=items,
            sequence=start_seq,
        )

        consumed = i - start_index
        return list_node, consumed

    def _strip_list_prefix(self, text: str) -> str:
        """Remove the bullet or number prefix from list item text."""
        # Try bullet patterns first
        bullet_match = self._LIST_BULLET_RE.match(text)
        if bullet_match:
            return text[bullet_match.end():].strip()

        # Try ordered patterns
        ordered_match = self._LIST_ORDERED_RE.match(text)
        if ordered_match:
            return text[ordered_match.end():].strip()

        return text

    # ── Helpers ────────────────────────────────────────────────────────

    def _flatten_elements(self, document: RawDocument) -> list[ExtractedElement]:
        """Flatten all page elements into a single reading-order list.

        After flattening, icons are reordered so that each icon appears
        immediately *before* the text element whose vertical range it
        overlaps with on the same page.  This corrects sub-pixel layout
        variations where the PDF parser places an icon slightly after
        its associated paragraph.
        """
        all_elements: list[ExtractedElement] = []
        for page in document.pages:
            all_elements.extend(page.elements)

        return self._reorder_icons(all_elements)

    @staticmethod
    def _vertical_overlap(bbox_a, bbox_b) -> float:
        """Return the vertical overlap ratio between two bounding boxes.

        Returns what fraction of the smaller box's height is overlapping.
        """
        if bbox_a is None or bbox_b is None:
            return 0.0
        if bbox_a.page != bbox_b.page:
            return 0.0
        overlap_top = max(bbox_a.y0, bbox_b.y0)
        overlap_bottom = min(bbox_a.y1, bbox_b.y1)
        overlap = max(0.0, overlap_bottom - overlap_top)
        if overlap == 0.0:
            return 0.0
        min_height = min(bbox_a.y1 - bbox_a.y0, bbox_b.y1 - bbox_b.y0)
        if min_height <= 0:
            return 0.0
        return overlap / min_height

    def _reorder_icons(self, elements: list[ExtractedElement]) -> list[ExtractedElement]:
        """Place each icon immediately before the text it vertically overlaps.

        Looks both forward and backward so a left-rail icon that was read
        before its paragraph still binds to that paragraph, not to a heading
        that happens to share the page.
        """
        # Only icons with a layout position (PDF) can be re-anchored. DOCX icons have
        # no bbox and are already in reading order, so they stay where they are.
        icons = [el for el in elements if el.element_type == ElementType.ICON and el.bbox is not None]
        if not icons:
            return list(elements)

        icon_ids = {id(ic) for ic in icons}
        rest = [el for el in elements if id(el) not in icon_ids]
        text_types = (
            ElementType.PARAGRAPH,
            ElementType.LIST_ITEM,
            ElementType.NUMBERED_STEP,
        )
        texts = [el for el in rest if el.element_type in text_types and el.bbox]
        assigned: set[int] = set()
        icon_to_text: dict[int, int] = {}

        for icon in icons:
            if icon.bbox is None:
                continue
            best = None
            best_score: tuple[float, float] | None = None
            for text in texts:
                if id(text) in assigned or text.bbox is None:
                    continue
                if text.page != icon.page:
                    continue
                overlap = self._vertical_overlap(icon.bbox, text.bbox)
                if overlap < 0.3:
                    continue
                if text.bbox.x1 < icon.bbox.x0:
                    continue
                hdist = max(0.0, text.bbox.x0 - icon.bbox.x1)
                score = (overlap, -hdist)
                if best_score is None or score > best_score:
                    best_score = score
                    best = text
            if best is not None:
                icon_to_text[id(icon)] = id(best)
                assigned.add(id(best))

        out: list[ExtractedElement] = []
        used: set[int] = set()
        for el in rest:
            matching = [ic for ic in icons if icon_to_text.get(id(ic)) == id(el)]
            matching.sort(key=lambda i: i.bbox.x0 if i.bbox else 0)
            out.extend(matching)
            used.update(id(i) for i in matching)
            out.append(el)

        leftover = [ic for ic in icons if id(ic) not in used]
        leftover.sort(key=lambda i: (i.page, i.bbox.y0 if i.bbox else 0, i.bbox.x0 if i.bbox else 0))
        for ic in leftover:
            inserted = False
            if ic.bbox is not None:
                for i, el in enumerate(out):
                    if el.bbox and el.page == ic.page and el.bbox.y0 > ic.bbox.y0:
                        out.insert(i, ic)
                        inserted = True
                        break
            if not inserted:
                out.append(ic)
        return out

    @staticmethod
    def _make_source_loc(el: ExtractedElement) -> SourceLocation:
        """Create a SourceLocation from an ExtractedElement."""
        return SourceLocation(
            page=el.page,
            bbox=el.bbox,
            paragraph_index=(getattr(el, "metadata", None) or {}).get("paragraph_index"),
        )

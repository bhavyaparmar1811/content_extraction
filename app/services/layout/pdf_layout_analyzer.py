"""PDF page layout analysis — column detection, header/footer zones, reading order.

This analyzer runs *after* the PDF parser extracts raw blocks and *before*
the AST builder assembles the hierarchy.  It takes the flat list of
``ExtractedElement`` objects on each page and:

1. Detects multi-column layouts via the XY-cut algorithm.
2. Identifies header / footer zones using vertical position heuristics.
3. Reconstructs the correct reading order across columns.
4. Groups blocks into spatial ``ContentRegion`` instances.

The output is a ``PageLayout`` per page, which the AST builder uses to
reorder and partition content.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

from loguru import logger as _default_logger

from app.config.settings import Settings
from app.schemas.document import (
    BoundingBox,
    ExtractedElement,
    ElementType,
    PageContent,
    RawDocument,
)
from app.schemas.layout import Column, ContentRegion, PageLayout
from app.services.layout.reading_order import Block, ReadingOrderAnalyzer


class PDFLayoutAnalyzer:
    """Analyze page layouts to detect columns and fix reading order.

    Parameters
    ----------
    settings : Settings
        Application settings.  Used for ``column_detection_enabled``
        and ``header_footer_zone_pct``.
    logger
        Optional logger override.
    """

    # Default percentage of page height considered header/footer zone
    _DEFAULT_HEADER_FOOTER_PCT = 0.08  # 8% of page height

    # Text this close to the page edge is a running header/footer on position
    # alone. Further in (up to 14% top / 12% bottom) it must match a pattern or
    # repeat across pages, so body text near the margin is kept.
    _EDGE_BAND_PCT = 0.06

    # Left-rail SOP icons are small images. Including them in XY-cut makes the
    # icon strip look like its own column, so headings and left fragments are
    # read before the right-hand paragraphs they belong with.
    _RAIL_ICON_MAX_PT = 90.0

    def __init__(self, settings: Settings, logger=None) -> None:
        self.settings = settings
        self.logger = logger or _default_logger
        self._reading_order = ReadingOrderAnalyzer(logger=self.logger)
        # Texts that repeat in the header/footer zone across pages (set per document).
        self._running_texts: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────

    def analyze_document(self, document: RawDocument) -> list[PageLayout]:
        """Run layout analysis on every page and return page layouts."""
        self._running_texts = self._find_running_texts(document)
        layouts: list[PageLayout] = []
        for idx, page in enumerate(document.pages):
            layout = self.analyze_page(page, is_first_page=(idx == 0))
            layouts.append(layout)
        return layouts

    def analyze_page(self, page: PageContent, is_first_page: bool = False) -> PageLayout:
        """Analyze a single page and return its ``PageLayout``.

        Steps:
        1. Separate header/footer blocks from body blocks.
        2. Run XY-cut reading order on body blocks.
        3. Build ``ContentRegion`` objects for each detected column.
        4. Return the layout with reordered blocks.
        """
        page_width = page.width or 612.0   # US Letter default
        page_height = page.height or 792.0

        # Step 1: separate headers/footers from body content
        header_elements, footer_elements, body_elements = self._detect_headers_footers(
            page.elements, page_height, is_first_page=is_first_page
        )

        # Left-rail icons are overlays, not a text column. Drop them from XY-cut.
        text_body = [el for el in body_elements if not self._is_rail_icon(el)]

        # Step 2: run reading order analysis on body text only
        ro_blocks = self._elements_to_blocks(text_body)
        ro_result = self._reading_order.compute_reading_order(
            ro_blocks, page_width, page_height
        )

        # Map ordered blocks back to elements
        block_id_to_element = {
            self._element_block_id(el, i): el
            for i, el in enumerate(text_body)
        }
        ordered_body_elements = []
        for block in ro_result.ordered_blocks:
            el = block_id_to_element.get(block.block_id)
            if el:
                ordered_body_elements.append(el)


        # Step 3: build column regions
        regions: list[ContentRegion] = []
        column_details: list[Column] = []

        if ro_result.column_count > 1:
            assigned_ids = set()
            for col_idx, (col_x0, col_x1) in enumerate(ro_result.column_boundaries):
                col_elements = []
                for el in ordered_body_elements:
                    if id(el) in assigned_ids:
                        continue
                    if el.bbox and self._element_in_column(el, col_x0, col_x1):
                        col_elements.append(el)
                        assigned_ids.add(id(el))
                
                region_id = f"col-{col_idx}"
                regions.append(ContentRegion(
                    region_id=region_id,
                    region_type="column",
                    bbox=BoundingBox(
                        x0=col_x0, y0=0,
                        x1=col_x1, y1=page_height,
                        page=page.page_number,
                    ),
                    blocks=col_elements,
                ))
                column_details.append(Column(
                    column_index=col_idx,
                    bbox=BoundingBox(
                        x0=col_x0, y0=0,
                        x1=col_x1, y1=page_height,
                        page=page.page_number,
                    ),
                    block_count=len(col_elements),
                ))
        else:
            # Single-column layout — wrap all body elements in one region
            region_id = "col-0"
            regions.append(ContentRegion(
                region_id=region_id,
                region_type="column",
                bbox=BoundingBox(
                    x0=0, y0=0,
                    x1=page_width, y1=page_height,
                    page=page.page_number,
                ),
                blocks=ordered_body_elements,
            ))
            column_details.append(Column(
                column_index=0,
                bbox=BoundingBox(
                    x0=0, y0=0,
                    x1=page_width, y1=page_height,
                    page=page.page_number,
                ),
                block_count=len(ordered_body_elements),
            ))

        reading_order = [r.region_id for r in regions]

        return PageLayout(
            page_number=page.page_number,
            width=page_width,
            height=page_height,
            columns=ro_result.column_count,
            column_details=column_details,
            regions=regions,
            reading_order=reading_order,
            headers=header_elements,
            footers=footer_elements,
        )

    def reorder_document(self, document: RawDocument) -> RawDocument:
        """Reorder elements in-place based on layout analysis.

        This modifies the ``document.pages`` elements list to reflect
        the correct reading order, with headers and footers separated.

        Returns the same RawDocument (mutated).
        """
        layouts = self.analyze_document(document)

        for page, layout in zip(document.pages, layouts):
            # Reassemble the elements list in reading order:
            # 1. Headers (kept at top if strip_headers_footers is False)
            # 2. Body elements in reading order from regions
            # 3. Footers (kept at bottom if strip_headers_footers is False)
            reordered: list[ExtractedElement] = []
            strip_hf = getattr(self.settings, "strip_headers_footers", True)
            header_ids = {id(el) for el in layout.headers}
            footer_ids = {id(el) for el in layout.footers}
            if not strip_hf:
                reordered.extend(layout.headers)
            for region in layout.regions:
                reordered.extend(region.blocks)
            if not strip_hf:
                reordered.extend(layout.footers)

            # 4. Re-insert left-rail icons immediately before the text they overlap.
            present_ids = {id(el) for el in reordered}
            rail_icons = [
                el for el in page.elements
                if self._is_rail_icon(el)
                and id(el) not in header_ids
                and id(el) not in footer_ids
                and id(el) not in present_ids
            ]
            reordered = self._insert_icons_before_text(reordered, rail_icons)
            reordered = self._group_inline_icons(reordered)

            # Re-sequence
            for i, el in enumerate(reordered):
                el.sequence = i

            page.elements = reordered

        return document

    # ── Header / Footer Detection ─────────────────────────────────────

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip().lower()

    def _find_running_texts(self, document: RawDocument) -> set[str]:
        """Texts in the top or bottom zone that recur on at least half the pages.

        Catches running-header values the patterns miss, such as the document
        title printed beside a separate "Title:" label.
        """
        pages = document.pages
        if len(pages) < 2:
            return set()
        page_counts: dict[str, int] = {}
        for page in pages:
            height = page.height or 792.0
            seen: set[str] = set()
            for el in page.elements:
                if el.bbox is None or el.element_type in ("table", "image"):
                    continue
                if el.bbox.y1 <= height * 0.14 or el.bbox.y0 >= height * 0.88:
                    text = self._norm_text(getattr(el, "content", ""))
                    if text:
                        seen.add(text)
            for text in seen:
                page_counts[text] = page_counts.get(text, 0) + 1
        threshold = max(2, len(pages) // 2)
        return {text for text, count in page_counts.items() if count >= threshold}

    def _is_running_header(
        self,
        el: ExtractedElement,
        page_height: float,
        is_first_page: bool = False,
    ) -> bool:
        """Hybrid check: true if element is in top zone and is a running header."""
        if el.element_type == "table":
            return False
        if el.bbox is None:
            return False

        # Must be in top 14% of page (y1 <= page_height * 0.14)
        if el.bbox.y1 > page_height * 0.14:
            return False

        if el.element_type == "image":
            return True

        text = (getattr(el, "content", "") or "").strip()
        if not text:
            return True

        header_patterns = [
            r"^Number:\s*",
            r"^BI-VQD-",
            r"^Boehringer\s+Ingelheim",
            r"^Page\s+\d+\s+of\s+\d+",
            r"^Title:\s*",
            r"^Document\s+Name:\s*",
            r"^Version:\s*",
            r"^Effective\s+Date:\s*",
            r"^Previous\s+Document\s+Number",
        ]
        if el.bbox.y1 <= page_height * self._EDGE_BAND_PCT:
            return True
        if self._norm_text(text) in self._running_texts:
            return True
        for pattern in header_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _is_running_footer(
        self,
        el: ExtractedElement,
        page_height: float,
    ) -> bool:
        """Hybrid check: true if element is in bottom zone and is a running footer."""
        if el.element_type == "table":
            return False
        if el.bbox is None:
            return False

        # Must be in bottom 12% of page (y0 >= page_height * 0.88)
        if el.bbox.y0 < page_height * 0.88:
            return False

        if el.element_type == "image":
            return True

        text = (getattr(el, "content", "") or "").strip()
        if not text:
            return True

        footer_patterns = [
            r"^Life\s+forward",
            r"Property\s+of\s+Boehringer\s+Ingelheim",
            r"Retrieved\s+by\s+",
            r"Verify\s+the\s+current\s+version",
            r"confidentiality\s+principles",
            r"Working\s+Copy",
            r"^Page\s+\d+\s+of\s+\d+",
            r"GMT[+-]\d+:\d+",
        ]
        if el.bbox.y0 >= page_height * (1 - self._EDGE_BAND_PCT):
            return True
        if self._norm_text(text) in self._running_texts:
            return True
        for pattern in footer_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _detect_headers_footers(
        self,
        elements: list[ExtractedElement],
        page_height: float,
        is_first_page: bool = False,
    ) -> tuple[list[ExtractedElement], list[ExtractedElement], list[ExtractedElement]]:
        """Separate elements into (headers, footers, body) using hybrid position + pattern matching."""
        headers: list[ExtractedElement] = []
        footers: list[ExtractedElement] = []
        body: list[ExtractedElement] = []

        for el in elements:
            if el.bbox is None:
                body.append(el)
                continue

            if self._is_running_header(el, page_height, is_first_page=is_first_page):
                headers.append(el)
            elif self._is_running_footer(el, page_height):
                footers.append(el)
            else:
                body.append(el)

        return headers, footers, body

    # ── Inline Icon Spatial Grouping ──────────────────────────────────

    def _is_rail_icon(self, el: ExtractedElement) -> bool:
        """True for small left-rail images/icons that should not form a column."""
        et = el.element_type
        is_media = et in (ElementType.IMAGE, ElementType.ICON, "image", "icon")
        if not is_media or el.bbox is None:
            return False
        width = el.bbox.x1 - el.bbox.x0
        height = el.bbox.y1 - el.bbox.y0
        return width <= self._RAIL_ICON_MAX_PT and height <= self._RAIL_ICON_MAX_PT

    @staticmethod
    def _y_overlap_ratio(a: BoundingBox, b: BoundingBox) -> float:
        overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
        if overlap <= 0:
            return 0.0
        min_h = min(a.y1 - a.y0, b.y1 - b.y0)
        if min_h <= 0:
            return 0.0
        return overlap / min_h

    def _is_icon_text_target(self, el: ExtractedElement) -> bool:
        et = el.element_type
        return et in (
            ElementType.PARAGRAPH,
            ElementType.LIST_ITEM,
            ElementType.NUMBERED_STEP,
            ElementType.LIST_ORDERED,
            ElementType.LIST_UNORDERED,
            "paragraph",
            "list_item",
            "numbered_step",
        )

    def _insert_icons_before_text(
        self,
        elements: list[ExtractedElement],
        icons: list[ExtractedElement],
    ) -> list[ExtractedElement]:
        """Place each rail icon immediately before the text it vertically overlaps."""
        if not icons:
            return elements

        texts = [el for el in elements if self._is_icon_text_target(el) and el.bbox]
        assigned_text_ids: set[int] = set()
        icon_to_text: dict[int, int] = {}

        for icon in icons:
            if icon.bbox is None:
                continue
            best = None
            best_score: tuple[float, float] | None = None
            for text in texts:
                if id(text) in assigned_text_ids or text.bbox is None:
                    continue
                if text.page != icon.page:
                    continue
                overlap = self._y_overlap_ratio(icon.bbox, text.bbox)
                if overlap < 0.25:
                    continue
                # Rail icons sit to the left of their text, not after it.
                if text.bbox.x1 < icon.bbox.x0:
                    continue
                hdist = max(0.0, text.bbox.x0 - icon.bbox.x1)
                score = (overlap, -hdist)
                if best_score is None or score > best_score:
                    best_score = score
                    best = text
            if best is not None:
                icon_to_text[id(icon)] = id(best)
                assigned_text_ids.add(id(best))

        out: list[ExtractedElement] = []
        used: set[int] = set()
        for el in elements:
            matching = [ic for ic in icons if icon_to_text.get(id(ic)) == id(el)]
            matching.sort(key=lambda i: i.bbox.x0 if i.bbox else 0)
            out.extend(matching)
            used.update(id(i) for i in matching)
            out.append(el)

        leftover = [ic for ic in icons if id(ic) not in used]
        leftover.sort(key=lambda i: (i.bbox.y0 if i.bbox else 0, i.bbox.x0 if i.bbox else 0))
        for ic in leftover:
            if ic.bbox is None:
                out.append(ic)
                continue
            inserted = False
            for i, el in enumerate(out):
                if el.bbox and el.bbox.y0 > ic.bbox.y0:
                    out.insert(i, ic)
                    inserted = True
                    break
            if not inserted:
                out.append(ic)
        return out

    def _group_inline_icons(self, elements: list[ExtractedElement]) -> list[ExtractedElement]:
        """Pull rail icons out of reading order and bind them to overlapping text."""
        icons = [el for el in elements if self._is_rail_icon(el)]
        if not icons:
            return elements
        rest = [el for el in elements if not self._is_rail_icon(el)]
        return self._insert_icons_before_text(rest, icons)

    # ── Conversion Helpers ────────────────────────────────────────────

    def _elements_to_blocks(
        self, elements: list[ExtractedElement],
    ) -> list[Block]:
        """Convert ExtractedElement list to Block list for XY-cut."""
        blocks: list[Block] = []
        for i, el in enumerate(elements):
            if el.bbox is None:
                continue
            blocks.append(Block(
                block_id=self._element_block_id(el, i),
                x0=el.bbox.x0,
                y0=el.bbox.y0,
                x1=el.bbox.x1,
                y1=el.bbox.y1,
                content=el.content,
            ))
        return blocks

    @staticmethod
    def _element_block_id(el: ExtractedElement, index: int) -> str:
        """Generate a stable block ID for an element."""
        return f"p{el.page}-s{el.sequence}-i{index}"

    @staticmethod
    def _element_in_column(
        el: ExtractedElement, col_x0: float, col_x1: float,
    ) -> bool:
        """Check if an element's midpoint falls within a column's x-range."""
        if el.bbox is None:
            return False
        mid_x = (el.bbox.x0 + el.bbox.x1) / 2.0
        return col_x0 <= mid_x <= col_x1

"""Tests for AST node types and the AST Builder.

Covers:
- AST node creation and serialization
- Section stack hierarchy building
- List item detection and consumption
- Table → TableNode conversion
- Image → ImageNode conversion
- Reading order algorithm
- PDF Layout Analyzer header/footer detection
"""

import json
import uuid
from typing import Optional

import pytest

from app.config.settings import Settings
from app.schemas.document import (
    BoundingBox,
    DocumentMetadata,
    ElementType,
    ExtractedElement,
    ExtractedHeading,
    ExtractedImage,
    ExtractedIcon,
    ExtractedTable,
    ExtractedTableCell,
    PageContent,
    RawDocument,
)
from app.schemas.ast_nodes import (
    ASTNode,
    DocumentNode,
    HeadingNode,
    HighlightSpan,
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
    CaptionNode,
)
from app.schemas.output import (
    DocumentExtractionOutput,
    ExtractionStats,
    AssetManifest,
    AssetReference,
)
from app.schemas.layout import Column, ContentRegion, PageLayout
from app.services.layout.reading_order import Block, ReadingOrderAnalyzer
from app.services.layout.pdf_layout_analyzer import PDFLayoutAnalyzer
from app.services.hierarchy.ast_builder import ASTBuilder


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def ast_builder(settings):
    return ASTBuilder(settings)


def _make_raw_doc(
    elements: list[ExtractedElement],
    title: str = "Test Document",
) -> RawDocument:
    """Helper: wrap elements in a single-page RawDocument."""
    return RawDocument(
        source="test.pdf",
        metadata=DocumentMetadata(title=title, page_count=1, file_type="pdf"),
        pages=[PageContent(page_number=1, elements=elements, width=612, height=792)],
    )


# ── AST Node Tests ───────────────────────────────────────────────────

class TestASTNodes:
    def test_document_node_defaults(self):
        node = DocumentNode()
        assert node.node_type == "document"
        assert node.node_id  # should have a UUID
        assert node.children == []

    def test_section_node_with_heading(self):
        heading = HeadingNode(text="Chapter 1", level=1, numbering="1")
        section = SectionNode(heading=heading, level=1)
        assert section.node_type == "section"
        assert section.heading.text == "Chapter 1"
        assert section.heading.numbering == "1"

    def test_paragraph_node_with_highlight(self):
        para = ParagraphNode(
            text="This is important text",
            highlights=[
                HighlightSpan(
                    text="important",
                    highlight_color="yellow",
                    color_hex="#FFFF00",
                    start_offset=8,
                    end_offset=17,
                )
            ],
        )
        assert len(para.highlights) == 1
        assert para.highlights[0].highlight_color == "yellow"

    def test_list_node_with_items(self):
        items = [
            ListItemNode(text="First item", index=1),
            ListItemNode(text="Second item", index=2),
        ]
        list_node = ListNode(list_type="ordered", items=items)
        assert list_node.list_type == "ordered"
        assert len(list_node.items) == 2

    def test_table_node_with_merged_cell(self):
        origin_id = str(uuid.uuid4())
        origin = TableCellNode(
            node_id=origin_id,
            row_index=0, col_index=0,
            row_span=1, col_span=2,
            is_merge_origin=True,
            content=[ParagraphNode(text="Merged Header")],
        )
        continuation = TableCellNode(
            row_index=0, col_index=1,
            row_span=1, col_span=1,
            is_merge_origin=False,
            merge_origin_ref=origin_id,
            content=[],
        )
        row = TableRowNode(row_index=0, is_header=True, cells=[origin, continuation])
        table = TableNode(row_count=1, col_count=2, grid_cols=2, rows=[row])
        assert table.rows[0].cells[0].col_span == 2
        assert table.rows[0].cells[1].merge_origin_ref == origin_id

    def test_image_node_with_ocr(self):
        img = ImageNode(
            asset_path="/images/test.png",
            width=640, height=480,
            ocr_text="Emergency Exit",
            ocr_confidence=0.92,
        )
        assert img.ocr_text == "Emergency Exit"
        assert img.ocr_confidence == 0.92

    def test_node_serialization_roundtrip(self):
        """All nodes should serialize to JSON and back."""
        doc = DocumentNode(
            doc_metadata=DocumentMetadata(title="Test"),
            children=[
                SectionNode(
                    level=1,
                    heading=HeadingNode(text="Heading 1", level=1),
                    children=[
                        ParagraphNode(text="Hello world"),
                        ImageNode(asset_path="img.png", width=100, height=100),
                    ],
                ),
            ],
        )
        json_str = doc.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["node_type"] == "document"
        assert len(parsed["children"]) == 1
        assert parsed["children"][0]["node_type"] == "section"
        assert len(parsed["children"][0]["children"]) == 2


# ── AST Builder Tests ────────────────────────────────────────────────

class TestASTBuilder:
    def test_empty_document(self, ast_builder):
        doc = _make_raw_doc([])
        ast = ast_builder.build(doc)
        assert ast.node_type == "document"
        assert ast.children == []

    def test_paragraphs_only(self, ast_builder):
        elements = [
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="Para 1", page=1, sequence=0),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="Para 2", page=1, sequence=1),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        assert len(ast.children) == 2
        assert all(isinstance(c, ParagraphNode) for c in ast.children)

    def test_heading_creates_section(self, ast_builder):
        elements = [
            ExtractedHeading(content="1. Introduction", page=1, sequence=0, level=1),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="Body text", page=1, sequence=1),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        assert len(ast.children) == 1
        section = ast.children[0]
        assert isinstance(section, SectionNode)
        assert section.level == 1
        assert section.heading.text == "1. Introduction"
        # Body paragraph should be a child of the section
        assert len(section.children) == 1
        assert isinstance(section.children[0], ParagraphNode)

    def test_nested_headings(self, ast_builder):
        elements = [
            ExtractedHeading(content="1. Chapter", page=1, sequence=0, level=1),
            ExtractedHeading(content="1.1 Section", page=1, sequence=1, level=2),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="Text", page=1, sequence=2),
            ExtractedHeading(content="2. Chapter Two", page=1, sequence=3, level=1),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        # Should have 2 top-level sections
        assert len(ast.children) == 2
        ch1 = ast.children[0]
        assert isinstance(ch1, SectionNode)
        assert ch1.heading.text == "1. Chapter"
        # ch1 should have a child section 1.1
        assert len(ch1.children) == 1
        assert isinstance(ch1.children[0], SectionNode)
        assert ch1.children[0].heading.text == "1.1 Section"
        # 1.1 should contain the paragraph
        assert len(ch1.children[0].children) == 1

    def test_table_conversion(self, ast_builder):
        elements = [
            ExtractedTable(
                content="Table",
                page=1,
                sequence=0,
                headers=[
                    ExtractedTableCell(content_text="Name"),
                    ExtractedTableCell(content_text="Age")
                ],
                rows=[
                    [ExtractedTableCell(content_text="Alice"), ExtractedTableCell(content_text="30")],
                    [ExtractedTableCell(content_text="Bob"), ExtractedTableCell(content_text="25")],
                ],
            ),
        ]

        raw_doc = RawDocument(
            source="test.pdf",
            metadata=DocumentMetadata(title="Test"),
            pages=[PageContent(page_number=1, elements=elements)],
        )

        ast = ast_builder.build(raw_doc)
        table = ast.children[0]

        assert isinstance(table, TableNode)
        assert table.row_count == 3  # 1 header + 2 data rows
        assert table.col_count == 2
        assert table.has_header_row is True
        assert len(table.rows) == 3
        
        # Check header
        assert table.rows[0].is_header is True
        assert len(table.rows[0].cells) == 2
        assert table.rows[0].cells[0].content[0].text == "Name"
        assert table.rows[0].cells[1].content[0].text == "Age"
        
        # Check data
        assert table.rows[1].is_header is False
        assert table.rows[1].cells[0].content[0].text == "Alice"
        assert table.rows[2].cells[1].content[0].text == "25"

    def test_image_conversion(self, ast_builder):
        elements = [
            ExtractedImage(
                content="[Image: test.png]",
                page=1,
                sequence=0,
                image_path="/images/test.png",
                width=640,
                height=480,
                caption="Figure 1",
            ),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        assert len(ast.children) == 1
        img = ast.children[0]
        assert isinstance(img, ImageNode)
        assert img.asset_path == "/images/test.png"
        assert img.width == 640

    def test_bullet_list_detection(self, ast_builder):
        elements = [
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="• First item", page=1, sequence=0),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="• Second item", page=1, sequence=1),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="• Third item", page=1, sequence=2),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="Regular paragraph", page=1, sequence=3),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        # First 3 should be consumed into a ListNode, then a paragraph
        assert len(ast.children) == 2
        list_node = ast.children[0]
        assert isinstance(list_node, ListNode)
        assert list_node.list_type == "unordered"
        assert len(list_node.items) == 3
        assert list_node.items[0].text == "First item"
        assert isinstance(ast.children[1], ParagraphNode)

    def test_ordered_list_detection(self, ast_builder):
        elements = [
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="1. Step one", page=1, sequence=0),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="2. Step two", page=1, sequence=1),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        assert len(ast.children) == 1
        list_node = ast.children[0]
        assert isinstance(list_node, ListNode)
        assert list_node.list_type == "ordered"
        assert len(list_node.items) == 2
        assert list_node.items[0].index == 1

    def test_heading_numbering_extraction(self, ast_builder):
        elements = [
            ExtractedHeading(content="3.2.1 Subsection Title", page=1, sequence=0, level=3),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        section = ast.children[0]
        assert section.heading.numbering == "3.2.1"

    def test_full_document_hierarchy(self, ast_builder):
        """Integration test: heading + paragraph + list + table + image."""
        elements = [
            ExtractedHeading(content="1. Safety Procedures", page=1, sequence=0, level=1),
            ExtractedElement(element_type=ElementType.PARAGRAPH, content="Follow these steps:", page=1, sequence=1),
            ExtractedElement(element_type=ElementType.LIST_ITEM, content="Wear PPE", page=1, sequence=2),
            ExtractedElement(element_type=ElementType.LIST_ITEM, content="Check equipment", page=1, sequence=3),
            ExtractedTable(
                content="Table", page=1, sequence=4,
                headers=[ExtractedTableCell(content_text="Item"), ExtractedTableCell(content_text="Required")],
                rows=[
                    [ExtractedTableCell(content_text="Goggles"), ExtractedTableCell(content_text="Yes")],
                    [ExtractedTableCell(content_text="Gloves"), ExtractedTableCell(content_text="Yes")]
                ],
            ),
            ExtractedImage(
                content="[Image]", page=1, sequence=5,
                image_path="ppe.png", width=300, height=200,
            ),
        ]
        ast = ast_builder.build(_make_raw_doc(elements))
        # One top-level section
        assert len(ast.children) == 1
        section = ast.children[0]
        assert isinstance(section, SectionNode)
        # Section should have: paragraph, list, table, image
        assert len(section.children) == 4
        assert isinstance(section.children[0], ParagraphNode)
        assert isinstance(section.children[1], ListNode)
        assert isinstance(section.children[2], TableNode)
        assert isinstance(section.children[3], ImageNode)

    def test_reorder_icons_binds_forward_overlapping_text(self, ast_builder):
        def box(x0, y0, x1, y1):
            return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1, page=1)

        elements = [
            ExtractedHeading(content="2 APPLICABILITY", level=1, page=1, sequence=0, bbox=box(72, 80, 500, 100)),
            ExtractedIcon(
                icon_id="buildings", image_path="b.png", page=1, sequence=1,
                bbox=box(72, 200, 110, 240),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH,
                content="Employees who perform change control.",
                page=1, sequence=2, bbox=box(120, 140, 500, 180),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH,
                content="All areas where changes are processed.",
                page=1, sequence=3, bbox=box(120, 200, 500, 240),
            ),
        ]
        ordered = ast_builder._reorder_icons(elements)
        contents = []
        for el in ordered:
            if el.element_type == ElementType.ICON:
                contents.append(el.image_path)
            else:
                contents.append(el.content)
        assert contents.index("b.png") == contents.index("All areas where changes are processed.") - 1
        assert contents.index("Employees who perform change control.") < contents.index("All areas where changes are processed.")


# ── Reading Order Tests ──────────────────────────────────────────────

class TestReadingOrder:
    def test_single_column(self):
        blocks = [
            Block("b1", 72, 100, 540, 120),
            Block("b2", 72, 130, 540, 150),
            Block("b3", 72, 160, 540, 180),
        ]
        analyzer = ReadingOrderAnalyzer()
        result = analyzer.compute_reading_order(blocks, 612, 792)
        assert result.column_count == 1
        assert len(result.ordered_blocks) == 3
        # Should be in top-to-bottom order
        assert result.ordered_blocks[0].block_id == "b1"

    def test_two_column_detection(self):
        # Left column blocks
        left = [
            Block("L1", 50, 100, 280, 120),
            Block("L2", 50, 130, 280, 150),
        ]
        # Right column blocks (with a clear gap between 280 and 320)
        right = [
            Block("R1", 320, 100, 550, 120),
            Block("R2", 320, 130, 550, 150),
        ]
        analyzer = ReadingOrderAnalyzer()
        result = analyzer.compute_reading_order(left + right, 612, 792)
        assert result.column_count == 2
        # Reading order should be: L1, L2, R1, R2 (left column first)
        ids = [b.block_id for b in result.ordered_blocks]
        assert ids.index("L1") < ids.index("R1")
        assert ids.index("L2") < ids.index("R1")

    def test_empty_blocks(self):
        analyzer = ReadingOrderAnalyzer()
        result = analyzer.compute_reading_order([], 612, 792)
        assert result.column_count == 1
        assert result.ordered_blocks == []


# ── PDF Layout Analyzer Tests ────────────────────────────────────────

class TestPDFLayoutAnalyzer:
    def test_header_footer_separation(self, settings):
        analyzer = PDFLayoutAnalyzer(settings)
        elements = [
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="Header text",
                page=1, sequence=0,
                bbox=BoundingBox(x0=72, y0=20, x1=540, y1=40, page=1),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="Body text",
                page=1, sequence=1,
                bbox=BoundingBox(x0=72, y0=200, x1=540, y1=220, page=1),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="Footer text",
                page=1, sequence=2,
                bbox=BoundingBox(x0=72, y0=760, x1=540, y1=780, page=1),
            ),
        ]
        page = PageContent(page_number=1, elements=elements, width=612, height=792)
        layout = analyzer.analyze_page(page)
        assert len(layout.headers) == 1
        assert layout.headers[0].content == "Header text"
        assert len(layout.footers) == 1
        assert layout.footers[0].content == "Footer text"
        # Body should have 1 region with 1 block
        body_blocks = []
        for region in layout.regions:
            body_blocks.extend(region.blocks)
        assert len(body_blocks) == 1
        assert body_blocks[0].content == "Body text"

    def test_body_text_near_margin_is_kept(self, settings):
        """Inside the 12% footer zone but off the page edge, plain text is body.

        Regression: the check read ``el.text`` (always empty), so every element
        in the zone was dropped, e.g. a bullet just above the footer.
        """
        analyzer = PDFLayoutAnalyzer(settings)
        bullet = ExtractedElement(
            element_type=ElementType.LIST_ITEM, content='• Use personal pronouns like "I" and "you".',
            page=1, sequence=0,
            bbox=BoundingBox(x0=72, y0=745, x1=540, y1=759, page=1),
        )
        footer = ExtractedElement(
            element_type=ElementType.PARAGRAPH, content="Retrieved by A on 03 Mar 2026 @ 16:39 GMT+00:00",
            page=1, sequence=1,
            bbox=BoundingBox(x0=72, y0=807, x1=540, y1=818, page=1),
        )
        page = PageContent(page_number=1, elements=[bullet, footer], width=595, height=842)
        layout = analyzer.analyze_page(page)
        body = [b.content for r in layout.regions for b in r.blocks]
        assert body == [bullet.content]
        assert [f.content for f in layout.footers] == [footer.content]

    def test_repeated_header_text_is_stripped(self, settings):
        """A header value with no pattern (the title beside a "Title:" label) repeats on every page."""
        analyzer = PDFLayoutAnalyzer(settings)
        pages = []
        for n in (1, 2, 3):
            pages.append(PageContent(page_number=n, width=595, height=842, elements=[
                ExtractedElement(
                    element_type=ElementType.PARAGRAPH, content="Good Writing Practice",
                    page=n, sequence=0, bbox=BoundingBox(x0=200, y0=80, x1=540, y1=95, page=n),
                ),
                ExtractedElement(
                    element_type=ElementType.PARAGRAPH, content=f"Body {n}",
                    page=n, sequence=1, bbox=BoundingBox(x0=72, y0=300, x1=540, y1=315, page=n),
                ),
            ]))
        doc = RawDocument(source="x.pdf", metadata=DocumentMetadata(title="x", page_count=3, file_type="pdf"), pages=pages)
        analyzer.reorder_document(doc)
        assert [[e.content for e in p.elements] for p in doc.pages] == [["Body 1"], ["Body 2"], ["Body 3"]]

    def test_reorder_document(self, settings):
        analyzer = PDFLayoutAnalyzer(settings)
        elements = [
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="B",
                page=1, sequence=1,
                bbox=BoundingBox(x0=72, y0=200, x1=540, y1=220, page=1),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="A",
                page=1, sequence=0,
                bbox=BoundingBox(x0=72, y0=100, x1=540, y1=120, page=1),
            ),
        ]
        doc = _make_raw_doc(elements)
        analyzer.reorder_document(doc)
        # After reorder, element with y0=100 should come first
        assert doc.pages[0].elements[0].content == "A"
        assert doc.pages[0].elements[1].content == "B"

    def test_rail_icons_keep_purpose_text_before_next_heading(self, settings):
        analyzer = PDFLayoutAnalyzer(settings)

        def box(x0, y0, x1, y1):
            return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1, page=1)

        elements = [
            ExtractedHeading(
                content="1 PURPOSE", level=1, page=1, sequence=0,
                bbox=box(72, 80, 500, 100),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="This SOP",
                page=1, sequence=1, bbox=box(72, 110, 150, 125),
            ),
            ExtractedImage(
                image_path="target.png", page=1, sequence=2,
                bbox=box(72, 140, 110, 180),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH,
                content="defines the change control process.",
                page=1, sequence=3, bbox=box(120, 140, 500, 185),
            ),
            ExtractedHeading(
                content="2 APPLICABILITY", level=1, page=1, sequence=4,
                bbox=box(72, 210, 500, 230),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH, content="This SOP is applicable:",
                page=1, sequence=5, bbox=box(72, 240, 400, 255),
            ),
            ExtractedImage(
                image_path="person.png", page=1, sequence=6,
                bbox=box(72, 270, 110, 310),
            ),
            ExtractedElement(
                element_type=ElementType.PARAGRAPH,
                content="Employees who perform change control.",
                page=1, sequence=7, bbox=box(120, 270, 500, 320),
            ),
        ]
        doc = _make_raw_doc(elements)
        analyzer.reorder_document(doc)
        texts = [
            el.content for el in doc.pages[0].elements
            if el.element_type in (ElementType.HEADING, ElementType.PARAGRAPH)
        ]
        assert texts.index("defines the change control process.") < texts.index("2 APPLICABILITY")

        define_idx = next(
            i for i, el in enumerate(doc.pages[0].elements)
            if el.content == "defines the change control process."
        )
        assert getattr(doc.pages[0].elements[define_idx - 1], "image_path", "").endswith("target.png")
        emp_idx = next(
            i for i, el in enumerate(doc.pages[0].elements)
            if "Employees" in (el.content or "")
        )
        assert getattr(doc.pages[0].elements[emp_idx - 1], "image_path", "").endswith("person.png")


# ── Output Schema Tests ──────────────────────────────────────────────

class TestOutputSchema:
    def test_extraction_output_serialization(self):
        output = DocumentExtractionOutput(
            document_id="test-123",
            extraction_timestamp="2024-01-01T00:00:00Z",
            ast=DocumentNode(
                doc_metadata=DocumentMetadata(title="Test"),
                children=[ParagraphNode(text="Hello")],
            ),
            extraction_stats=ExtractionStats(
                total_paragraphs=1,
                total_sections=0,
            ),
        )
        data = json.loads(output.model_dump_json())
        assert data["version"] == "2.0"
        assert data["document_id"] == "test-123"
        assert data["ast"]["node_type"] == "document"
        assert len(data["ast"]["children"]) == 1
        assert data["extraction_stats"]["total_paragraphs"] == 1

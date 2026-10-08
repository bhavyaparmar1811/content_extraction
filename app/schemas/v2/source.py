"""Canonical source model: ordered sections and traceable source units."""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Optional

from pydantic import Field, model_validator

from .common import SCHEMA_VERSION, HexColor, V2Model
from .refs import CrossReference


_WS = re.compile(r"\s+")


def compute_content_hash(text: str, structural_path: str = "") -> str:
    """Stable hash of normalized text plus structural position.

    Used to match units across re-uploads, where display IDs may shift.
    """
    normalized = _WS.sub(" ", text).strip().lower()
    return hashlib.sha256(f"{structural_path}|{normalized}".encode("utf-8")).hexdigest()[:16]


class UnitType(str, Enum):
    PARAGRAPH = "paragraph"
    PROCEDURE_STEP = "procedure_step"
    BULLET = "bullet"
    WARNING = "warning"
    NOTE = "note"
    TABLE_ROW = "table_row"
    TABLE_CELL_GROUP = "table_cell_group"
    CAPTION = "caption"
    REFERENCE = "reference"
    DEFINITION = "definition"
    HEADING_STATEMENT = "heading_statement"
    FIGURE = "figure"  # a diagram or picture; its caption is a separate CAPTION unit


class RelationType(str, Enum):
    WARNING_FOR = "warning_for"
    NOTE_FOR = "note_for"
    CONDITION_OF = "condition_of"
    CONTINUES = "continues"  # e.g. a table row split across pages
    CAPTION_OF = "caption_of"


class UnitRelation(V2Model):
    type: RelationType
    target_unit_id: str


class SourceLocation(V2Model):
    page: Optional[int] = None
    paragraph_index: Optional[int] = None
    xml_path: Optional[str] = None


class AssetKind(str, Enum):
    FIGURE = "figure"  # content image: keep it, with its caption
    ICON = "icon"  # small marker beside text: metadata only, never content


class SourceAsset(V2Model):
    kind: AssetKind
    asset_key: str = Field(description="'icon_<md5[:10]>' of the image bytes, as in the template icon library")
    path: Optional[str] = None
    width_px: Optional[int] = Field(default=None, ge=0)
    height_px: Optional[int] = Field(default=None, ge=0)


class TableCell(V2Model):
    col: int
    text: str
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    fill_hex: Optional[HexColor] = Field(default=None, description="Meaningful cell shading; header fills are dropped")


class TableRef(V2Model):
    table_id: str
    row_index: int
    header_cells: list[str] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)


class SourceUnit(V2Model):
    unit_id: str = Field(description="Display ID, e.g. 'SRC-4.2-U003'")
    content_hash: str
    section_id: str
    seq: int = Field(ge=0, description="Reading order within the whole document")
    unit_type: UnitType
    text: str
    list_level: Optional[int] = Field(default=None, ge=0)
    list_number: Optional[str] = None
    table_ref: Optional[TableRef] = None
    location: SourceLocation = Field(default_factory=SourceLocation)
    relations: list[UnitRelation] = Field(default_factory=list)
    assets: list[SourceAsset] = Field(
        default_factory=list, description="Figure image, or the icon shown beside this unit in the source"
    )
    is_boilerplate: bool = False

    @model_validator(mode="after")
    def _table_rows_need_table_ref(self) -> "SourceUnit":
        if self.unit_type == UnitType.TABLE_ROW and self.table_ref is None:
            raise ValueError("table_row units must carry a table_ref")
        has_figure = any(a.kind == AssetKind.FIGURE for a in self.assets)
        if self.unit_type == UnitType.FIGURE and not has_figure:
            raise ValueError("figure units must carry a figure asset")
        if has_figure and self.unit_type != UnitType.FIGURE:
            raise ValueError("only figure units may carry a figure asset")
        return self


class SourceSection(V2Model):
    section_id: str = Field(description="e.g. 'SRC-4.2'")
    number: Optional[str] = Field(default=None, description="Numbering as printed, e.g. '4.2'")
    heading: str
    level: int = Field(ge=0)
    section_order: int = Field(ge=0)
    parent_id: Optional[str] = None
    units: list[SourceUnit] = Field(default_factory=list)


class SourceDocument(V2Model):
    schema_version: str = SCHEMA_VERSION
    document_id: str
    document_version: Optional[str] = None
    source_file: str
    file_type: str
    language: str = "en"
    sections: list[SourceSection] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)

    def iter_units(self):
        for section in self.sections:
            yield from section.units

    @model_validator(mode="after")
    def _unique_ids(self) -> "SourceDocument":
        section_ids = [s.section_id for s in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("duplicate section_id in SourceDocument")
        unit_ids = [u.unit_id for u in self.iter_units()]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("duplicate unit_id in SourceDocument")
        return self

"""Canonical target template model: sections and semantic instruction slots."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field, model_validator

from .common import SCHEMA_VERSION, CalloutKind, ContentType, HexColor, V2Model


class AnchorKind(str, Enum):
    CONTENT_CONTROL = "content_control"
    BOOKMARK = "bookmark"
    TABLE_CELL = "table_cell"
    PLACEHOLDER = "placeholder"
    PARAGRAPH = "paragraph"  # proximity fallback, never "ready" on its own


class InstructionBehavior(str, Enum):
    """What happens to the template instruction after population.

    Set from template metadata or config, never by the LLM.
    """

    RETAIN_AS_LABEL = "retain_as_label"
    REPLACE = "replace"
    HIDE_AFTER_POPULATION = "hide_after_population"
    RETAIN_SEPARATE = "retain_separate"  # instruction kept, content goes to an adjacent anchor


class SlotAnchor(V2Model):
    kind: AnchorKind
    ref: str = Field(description="Tag, bookmark name, placeholder token or paragraph index")
    table_index: Optional[int] = None
    row_index: Optional[int] = None
    column_index: Optional[int] = None

    @model_validator(mode="after")
    def _table_cell_needs_coordinates(self) -> "SlotAnchor":
        if self.kind == AnchorKind.TABLE_CELL and None in (self.table_index, self.row_index, self.column_index):
            raise ValueError("table_cell anchors need table_index, row_index and column_index")
        return self


class SlotIcon(V2Model):
    icon_key: str = Field(description="Key into the template's icon_library")
    type: Optional[str] = Field(default=None, description="e.g. 'person', 'clock'")
    relationship: str = "visual_marker"


class FormattingProfile(str, Enum):
    PARAGRAPHS = "paragraphs"
    ORDERED_LIST = "ordered_list"
    BULLETS = "bullets"
    TABLE = "table"
    CALLOUT = "callout"


class CalloutLayout(str, Enum):
    ICON_TEXT_TWO_CELL = "icon_text_two_cell"
    SINGLE_CELL = "single_cell"


class CalloutOrigin(str, Enum):
    LEGEND = "legend"  # the template's "Infographics | Description" table
    PROTOTYPE = "prototype"  # a callout box placed in the template body
    CONFIG = "config"  # data/template_config override


class CalloutStyle(V2Model):
    """One entry of the template's callout palette.

    Rendering clones the prototype table when there is one, so fills, icon and
    layout match the template exactly; the other fields are the fallback.
    """

    kind: CalloutKind
    label: str
    fill_hex: HexColor
    icon_key: Optional[str] = None
    text_color_hex: Optional[HexColor] = None
    layout: CalloutLayout = CalloutLayout.ICON_TEXT_TWO_CELL
    column_widths_pt: list[float] = Field(default_factory=list)
    prototype_table_index: Optional[int] = Field(default=None, ge=0, description="Body table index of a box to clone")
    origin: CalloutOrigin = CalloutOrigin.LEGEND


class TargetSlot(V2Model):
    slot_id: str = Field(description="e.g. 'TGT-3-RESPONSIBILITY'")
    section_id: str
    key: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$", description="Stable name within the section, e.g. 'geography'"
    )
    instruction: str
    content_type: ContentType
    required: bool = False
    display_order: int = Field(ge=0)
    anchor: Optional[SlotAnchor] = Field(default=None, description="None until detected or normalized")
    instruction_behavior: InstructionBehavior = InstructionBehavior.REPLACE
    icon: Optional[SlotIcon] = None
    formatting_profile: Optional[FormattingProfile] = None
    callout_kind: Optional[CalloutKind] = Field(default=None, description="Set for fixed callout slots")
    instruction_ids: list[str] = Field(
        default_factory=list, description="Linked TemplateInstruction IDs from the v2.0 template extraction"
    )


class ConditionalKind(str, Enum):
    INLINE_CHOICE = "inline_choice"  # black text with a blue choice, e.g. "This Directive/SOP/...:" → "This SOP:"
    BLOCK = "block"  # a paragraph or table kept or removed depending on the source SOP


class ConditionalRegion(V2Model):
    """Template content whose fate depends on the SOP being migrated, decided per migration."""

    region_id: str = Field(description="'p:<n>' or 't:<n>' in the original template")
    kind: ConditionalKind
    text: str
    anchor: Optional[SlotAnchor] = Field(default=None, description="Content control after normalization")


class TargetSection(V2Model):
    section_id: str = Field(description="e.g. 'TGT-3'")
    key: Optional[str] = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9_]*$", description="Stable name, e.g. 'APPLICABILITY'; also the CC tag prefix"
    )
    number: Optional[str] = None
    heading: str
    level: int = Field(ge=0)
    display_order: int = Field(ge=0)
    required: bool = False
    purpose_instruction: Optional[str] = None
    optional_marker: bool = Field(default=False, description="Heading carries a highlighted '(optional)'")
    aliases: list[str] = Field(
        default_factory=list, description="Other source headings for this section, e.g. 'SCOPE' for APPLICABILITY (config)"
    )
    slots: list[TargetSlot] = Field(default_factory=list)
    one_of: list[list[str]] = Field(
        default_factory=list,
        description="Groups of slot keys of which at least one must be filled, e.g. [['roles', 'raci']] for 'table A, B, or both'",
    )
    conditional_regions: list[ConditionalRegion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_of_names_slots(self) -> "TargetSection":
        keys = {s.key for s in self.slots}
        for group in self.one_of:
            if len(group) < 2 or len(set(group)) != len(group):
                raise ValueError(f"section {self.section_id}: a one_of group needs two or more distinct slot keys")
            missing = [k for k in group if k not in keys]
            if missing:
                raise ValueError(f"section {self.section_id}: one_of names unknown slots {missing}")
        return self


class ReadinessStatus(str, Enum):
    READY = "ready"
    NEEDS_NORMALIZATION = "needs_normalization"
    NEEDS_REVIEW = "needs_review"


class TemplateModel(V2Model):
    schema_version: str = SCHEMA_VERSION
    template_id: str
    template_version: int = Field(ge=1)
    source_file: str
    normalized_file: Optional[str] = None
    sections: list[TargetSection] = Field(default_factory=list)
    callout_palette: list[CalloutStyle] = Field(default_factory=list)
    readiness: ReadinessStatus = ReadinessStatus.NEEDS_REVIEW

    def iter_slots(self):
        for section in self.sections:
            yield from section.slots

    def slot_by_ref(self, ref: str) -> Optional[TargetSlot]:
        """Look up a slot by ``<SECTION_KEY>.<slot_key>``, e.g. ``APPLICABILITY.geography``."""
        section_key, _, slot_key = ref.partition(".")
        for section in self.sections:
            if section.key == section_key:
                return next((s for s in section.slots if s.key == slot_key), None)
        return None

    @model_validator(mode="after")
    def _consistent(self) -> "TemplateModel":
        slot_ids = [s.slot_id for s in self.iter_slots()]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("duplicate slot_id in TemplateModel")
        section_keys = [s.key for s in self.sections if s.key]
        if len(section_keys) != len(set(section_keys)):
            raise ValueError("duplicate section key in TemplateModel")
        for section in self.sections:
            slot_keys = [s.key for s in section.slots if s.key]
            if len(slot_keys) != len(set(slot_keys)):
                raise ValueError(f"duplicate slot key in section {section.section_id}")
            for slot in section.slots:
                if slot.section_id != section.section_id:
                    raise ValueError(f"slot {slot.slot_id} is listed under {section.section_id} but says {slot.section_id}")
        palette = {}
        for style in self.callout_palette:
            if style.kind in palette:
                raise ValueError(f"duplicate callout kind in palette: {style.kind.value}")
            palette[style.kind] = style
        for slot in self.iter_slots():
            is_callout = slot.formatting_profile == FormattingProfile.CALLOUT
            if is_callout != (slot.callout_kind is not None):
                raise ValueError(f"slot {slot.slot_id}: formatting_profile 'callout' and callout_kind must be set together")
            if slot.callout_kind is not None and slot.callout_kind not in palette:
                raise ValueError(f"slot {slot.slot_id} uses callout kind {slot.callout_kind.value} missing from the palette")
        if self.readiness == ReadinessStatus.READY:
            no_prototype = sorted(
                {s.callout_kind.value for s in self.iter_slots() if s.callout_kind is not None}
                - {k.value for k, st in palette.items() if st.prototype_table_index is not None}
            )
            if no_prototype:
                raise ValueError(f"template cannot be ready with callout kinds lacking a prototype: {no_prototype}")
            unanchored = [s.slot_id for s in self.iter_slots() if s.anchor is None or s.anchor.kind == AnchorKind.PARAGRAPH]
            if unanchored:
                raise ValueError(f"template cannot be ready with unanchored slots: {unanchored}")
            unanchored_regions = [
                r.region_id for s in self.sections for r in s.conditional_regions
                if r.anchor is None or r.anchor.kind != AnchorKind.CONTENT_CONTROL
            ]
            if unanchored_regions:
                raise ValueError(f"template cannot be ready with unanchored conditional regions: {unanchored_regions}")
        return self

"""Write a normalized copy of a template: every slot destination in a tagged content control (Phase 3b).

Tags are ``CC_<SECTION_KEY>_<SLOT_KEY>``, for example ``CC_APPLICABILITY_GEOGRAPHY``.
How each kind of destination is wrapped:

- a group of instruction paragraphs → one block-level content control around them;
- a table cell (icon row, callout box) → a content control around the cell's paragraphs;
- a data table's example rows → a row-level content control around each row,
  all with the slot's tag (the first is the row to clone). A control spanning
  several rows is split into its own table when Word saves the file;
- a conditional region (kept or removed per SOP) → a block content control
  tagged ``COND_<SECTION_KEY>_<n>``.

Bookmark anchors are already stable and stay as they are. Region IDs (``p:<n>``
and ``t:<n>``) don't change, because ``iter_body_blocks`` flattens block-level
content controls. The original file is never modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app.schemas.v2 import AnchorKind, SlotAnchor

from .slot_detector import COND_PREFIX, TAG_PREFIX, DetectionResult


@dataclass
class NormalizationResult:
    path: Path
    wrapped: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (slot_id, reason)


def slot_tag(section_key: str, slot_key: str) -> str:
    return f"{TAG_PREFIX}{section_key}_{slot_key.upper()}"


def normalize_template(detection: DetectionResult, out_path: str | Path) -> NormalizationResult:
    """Wrap the slot regions of *detection* and save the document to *out_path*.

    Updates the anchors in ``detection.model`` to the new content controls.
    """
    out_path = Path(out_path)
    result = NormalizationResult(path=out_path)
    ids = _IdAllocator(detection.document.element)
    sections = {s.section_id: s for s in detection.model.sections}

    for slot in detection.model.iter_slots():
        anchor = slot.anchor
        if anchor is not None and anchor.kind in (AnchorKind.CONTENT_CONTROL, AnchorKind.BOOKMARK):
            continue
        region = detection.regions.get(slot.slot_id)
        if region is None or not region.elements:
            result.skipped.append((slot.slot_id, "no region to wrap"))
            continue
        tag = slot_tag(sections[slot.section_id].key, slot.key)
        alias = f"{sections[slot.section_id].heading}: {slot.key}"
        try:
            if region.kind == "paragraphs":
                _wrap_siblings(region.elements, tag, alias, ids)
            elif region.kind == "cell":
                _wrap_cell(region.elements[0], tag, alias, ids)
            elif region.kind == "rows":
                # One control per row, all with the slot's tag: Word splits a control
                # spanning several rows into a separate table when it saves.
                for tr in region.elements:
                    _wrap_siblings([tr], tag, alias, ids)
            else:
                result.skipped.append((slot.slot_id, f"region kind {region.kind}"))
                continue
        except _CannotWrap as exc:
            result.skipped.append((slot.slot_id, str(exc)))
            continue
        slot.anchor = SlotAnchor(
            kind=AnchorKind.CONTENT_CONTROL,
            ref=tag,
            table_index=anchor.table_index if anchor else None,
            row_index=anchor.row_index if anchor else None,
            column_index=anchor.column_index if anchor else None,
        )
        result.wrapped.append(slot.slot_id)

    for section in detection.model.sections:
        for n, region in enumerate(section.conditional_regions, start=1):
            if region.anchor is not None and region.anchor.kind == AnchorKind.CONTENT_CONTROL:
                continue
            element = detection.conditional_elements.get(region.region_id)
            if element is None:
                result.skipped.append((region.region_id, "no conditional element to wrap"))
                continue
            tag = f"{COND_PREFIX}{section.key}_{n}"
            _wrap_siblings([element], tag, f"{section.heading}: conditional {n}", ids)
            previous = region.anchor
            region.anchor = SlotAnchor(
                kind=AnchorKind.CONTENT_CONTROL,
                ref=tag,
                table_index=previous.table_index if previous else None,
                row_index=previous.row_index if previous else None,
                column_index=previous.column_index if previous else None,
            )
            result.wrapped.append(region.region_id)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    detection.document.save(str(out_path))
    detection.model.normalized_file = str(out_path)
    return result


class _CannotWrap(Exception):
    pass


class _IdAllocator:
    """Content-control IDs that don't collide with the document's own."""

    def __init__(self, root):
        used = []
        for node in root.iter(qn("w:id")):
            try:
                used.append(int(node.get(qn("w:val"))))
            except (TypeError, ValueError):
                pass
        self._next = max([0x10000] + [u + 1 for u in used if 0 <= u < 2**31 - 1000])

    def take(self) -> int:
        value, self._next = self._next, self._next + 1
        return value


def _sdt(tag: str, alias: str, ids: _IdAllocator):
    sdt = OxmlElement("w:sdt")
    pr = OxmlElement("w:sdtPr")
    for name, value in (("w:alias", alias[:255]), ("w:tag", tag), ("w:id", str(ids.take()))):
        node = OxmlElement(name)
        node.set(qn("w:val"), value)
        pr.append(node)
    sdt.append(pr)
    content = OxmlElement("w:sdtContent")
    sdt.append(content)
    return sdt, content


def _wrap_siblings(elements: list, tag: str, alias: str, ids: _IdAllocator) -> None:
    """Move *elements*, and everything between them, into one content control."""
    parent = elements[0].getparent()
    if any(el.getparent() is not parent for el in elements):
        raise _CannotWrap("region spans different parents")
    children = list(parent)
    start, end = children.index(elements[0]), children.index(elements[-1])
    sdt, content = _sdt(tag, alias, ids)
    parent.insert(start, sdt)
    for child in children[start:end + 1]:
        content.append(child)


def _wrap_cell(tc, tag: str, alias: str, ids: _IdAllocator) -> None:
    blocks = [child for child in tc if child.tag != qn("w:tcPr")]
    if not blocks:
        raise _CannotWrap("empty cell")
    sdt, content = _sdt(tag, alias, ids)
    tc.append(sdt)
    for child in blocks:
        content.append(child)

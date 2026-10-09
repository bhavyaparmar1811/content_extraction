"""Template instructions out, conditional regions settled (Phase 11).

The GP templates say "follow the instructions in blue text and delete the blue
text before finalization". After the slots are filled, every blue paragraph left
in the body is an instruction and goes, with the blue spacer lines between
instructions and tables written entirely in blue. Blue runs inside a black
paragraph go; the black text stays. Rendered content, retained instructions and
the table of contents and headings are never touched. Blue is decided by ``TextColour``,
exactly as slot detection decided it.

Conditional regions are settled from the slot plan's ``region_choices``:
- an inline choice ("This Directive/SOP/Work Instruction/Guidance:") takes the
  option the source uses ("This SOP:"). Without a choice of its own it takes the
  document type another region of the same SOP settled; with none it is removed;
- a block region (e.g. a competence table) is kept only when the slot plan keeps
  it, and then turns black.
"""

from __future__ import annotations

import re
from typing import Optional

from docx.oxml.ns import qn

from app.schemas.v2 import (
    AnchorKind,
    ConditionalKind,
    RegionOutcome,
    RegionRender,
    SlotPlan,
    TemplateModel,
)
from app.services.parser.ooxml import is_toc_sdt

from ..template.callout_palette import is_heading
from ..template.colour import TextColour, is_blue, run_text, style_id
from .xml import (
    W_P,
    W_R,
    W_SDT,
    W_SDT_CONTENT,
    W_TBL,
    W_TC,
    ancestor,
    base_rpr,
    ensure_ends_with_paragraph,
    make_plain,
    paragraph,
    remove,
    unwrap,
)

_CHOICE_GROUP = re.compile(r"[\w ]+(?:/[\w ]+)+")
_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*")


# ── Conditional regions ───────────────────────────────────────────────


def settle_regions(template: TemplateModel, slot_plan: Optional[SlotPlan], controls: dict[str, list]) -> list[RegionRender]:
    """Apply, keep or remove each conditional region; the region's content control is unwrapped."""
    choices = {rc.region_id: rc for s in (slot_plan.sections if slot_plan else []) for rc in s.region_choices}
    document_choice = next((rc.choice for rc in choices.values() if rc.choice), None)
    out = []
    for section in template.sections:
        for region in section.conditional_regions:
            anchor = region.anchor
            sdts = controls.get(anchor.ref, []) if anchor and anchor.kind == AnchorKind.CONTENT_CONTROL else []
            if not sdts:
                out.append(RegionRender(region_id=region.region_id, outcome=RegionOutcome.REMOVED,
                                        note="its content control is not in the template file"))
                continue
            chosen = choices.get(region.region_id)
            if region.kind == ConditionalKind.INLINE_CHOICE:
                choice = chosen.choice if chosen and chosen.choice else document_choice
                if choice:
                    for sdt in sdts:
                        for p in list(sdt.iter(W_P)):
                            _apply_choice(p, choice)
                        unwrap(sdt)
                    note = None if chosen and chosen.choice else "document type taken from another region of this SOP"
                    out.append(RegionRender(region_id=region.region_id, outcome=RegionOutcome.CHOICE_APPLIED,
                                            choice=choice, note=note))
                    continue
                note = "no source line answers it"
            else:
                if chosen is not None:
                    for sdt in sdts:
                        make_plain(sdt)
                        unwrap(sdt)
                    out.append(RegionRender(region_id=region.region_id, outcome=RegionOutcome.KEPT))
                    continue
                note = "kept only when the slot plan keeps it"
            for sdt in sdts:
                _remove_block(sdt)
            out.append(RegionRender(region_id=region.region_id, outcome=RegionOutcome.REMOVED, note=note))
    return out


def _apply_choice(p, choice: str) -> None:
    """"This Directive/SOP/Work Instruction/Guidance…is applicable:" → "This SOP is applicable:" in plain text."""
    rpr = None
    parts = []
    for r in p.iter(W_R):
        text = run_text(r)
        if not text:
            continue
        colour = r.find(f"{qn('w:rPr')}/{qn('w:color')}")
        if rpr is None and not is_blue(colour.get(qn("w:val")) if colour is not None else None):
            rpr = base_rpr(r)
        text = _CHOICE_GROUP.sub(lambda m: (" " if m.group(0).startswith(" ") else "") + choice, text, count=1)
        parts.append(_ELLIPSIS.sub(" ", text))
    line = re.sub(r"\s+([:;,.])", r"\1", re.sub(r"\s+", " ", "".join(parts))).strip()
    for r in list(p.iter(W_R)):
        remove(r)
    for r in paragraph(line, None, rpr).findall(W_R):
        p.append(r)
    make_plain(p)


# ── Instruction cleanup ───────────────────────────────────────────────


def remove_instructions(doc, colour: TextColour, keep: set) -> int:
    """Remove the blue instruction text left in the body; *keep* holds elements that must stay. Returns removals."""
    body = doc.element.body
    removed = 0
    for tbl in list(body.iter(W_TBL)):
        if tbl in keep or _in_toc(tbl) or ancestor(tbl, W_TC) is not None and ancestor(tbl, W_TC) in keep:
            continue
        if colour.classify(tbl) == "blue" and next(tbl.iter(qn("w:drawing")), None) is None:
            _remove_block(tbl)
            removed += 1
    for p in list(body.iter(W_P)):
        if p in keep or p.getparent() is None or _in_toc(p) or is_heading(doc, p):
            continue  # headings are structure, whatever colour their style has
        kind = colour.classify(p)
        if kind == "blue" or (kind is None and _is_blank(p) and colour.mark_is_blue(p)):
            _remove_paragraph(p)
            removed += 1
        elif kind == "mixed":
            p_style = style_id(p, "pPr", "pStyle")
            for r in list(p.iter(W_R)):
                if run_text(r).strip() and colour.run_is_blue(r, p_style):
                    remove(r)
            removed += 1
    for sdt in list(body.iter(W_SDT)):
        content = sdt.find(W_SDT_CONTENT)
        if content is not None and not len(content) and sdt.getparent() is not None:
            parent = sdt.getparent()
            remove(sdt)
            if parent.tag == W_TC:
                ensure_ends_with_paragraph(parent)
    return removed


def _is_blank(p) -> bool:
    return (not "".join(t.text or "" for t in p.iter(qn("w:t"))).strip()
            and next(p.iter(qn("w:drawing")), None) is None and next(p.iter(qn("w:pict")), None) is None
            and next(p.iter(qn("w:fldChar")), None) is None)


def _in_toc(el) -> bool:
    parent = el.getparent()
    while parent is not None:
        if parent.tag == W_SDT and is_toc_sdt(parent):
            return True
        parent = parent.getparent()
    return False


def _remove_paragraph(p) -> None:
    """Remove a paragraph; one that carries a section break, or is the last block of a cell, is emptied instead."""
    parent = p.getparent()
    if p.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is not None:
        for child in list(p):
            if child.tag != qn("w:pPr"):
                p.remove(child)
        return
    remove(p)
    cell = parent if parent.tag == W_TC else ancestor(parent, W_TC) if parent.tag == W_SDT_CONTENT else None
    if cell is not None:
        ensure_ends_with_paragraph(parent if parent.tag == W_TC else cell)


def _remove_block(el) -> None:
    """Remove a block (table, paragraph or content control) and keep its cell valid."""
    parent = el.getparent()
    if parent is None:
        return
    remove(el)
    if parent.tag == W_TC:
        ensure_ends_with_paragraph(parent)

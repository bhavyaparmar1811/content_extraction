"""Anchor-based Word renderer (Phase 11, Level 4): the drafts into a copy of the normalized template.

Every slot's claims go in place at its anchor: a tagged content control
(``CC_<SECTION>_<SLOT>``), a bookmark, a table cell or a placeholder. Nothing is
appended at the end of the document and moved. In order:

1. conditional regions are settled (``instructions.settle_regions``);
2. each slot is filled, shows a gap marker, or is removed:
   - content → the anchor's instruction is replaced (``retain_as_label`` keeps it
     above the content, plain); table slots fill the template table;
   - a required slot without source content → a visible "Source content not
     found. Human review required." marker (review draft). The final export
     removes it, with its instruction, once the reviewer accepted it as N/A,
     and is refused while any gap is unresolved;
   - an optional slot without content → removed with its instruction (a table
     cell's row, a callout box, a data table, a block);
3. optional sections without content are removed; the highlighted "(optional)"
   leaves the heading of a section that has content; the callout legend goes;
4. the remaining blue instruction text is removed (``instructions``);
5. the final export unwraps the slot content controls; drawing IDs are made
   unique;
6. chapters and sub-headings get the bookmarks of the ``NumberMap`` (Phase 12); a resolved reference shows its
   number as a REF field to them (``assembled_ref_text``), and the post-render check compares Word's numbering of
   each bookmarked heading with the map;
7. the table of contents is rebuilt from the rendered headings (``toc``), with
   page numbers measured by Word when a page counter is given (``pages``);
   without them Word is asked to refresh the fields on opening.

Headers, footers, the cover page and the template's icons are not touched.
Without an ``AssembledDocument``, reference tokens (``{{ref:...}}``) show the
source's wording.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from docx import Document
from docx.oxml.ns import qn

from app.schemas.v2 import (
    AnchorKind,
    AssembledDocument,
    NumberKind,
    NumberMap,
    Claim,
    ClaimKind,
    FormattingProfile,
    InstructionBehavior,
    RenderMode,
    RenderReport,
    SectionDraft,
    SlotOutcome,
    SlotPlan,
    SlotRender,
    SourceDocument,
    TargetSection,
    TargetSlot,
    TemplateModel,
)
from app.services.parser.ooxml import NumberingResolver, iter_body_blocks

from ..drafting.refs import raw_text_by_target
from ..template.callout_palette import is_heading, palette_from_document, style_name
from ..template.colour import TextColour
from .callouts import Callouts
from .content import ContentContext, RefText, build_blocks, gap_marker
from .instructions import remove_instructions, settle_regions
from .numbering import ListNumbering
from .pages import PageCounter
from .tables import (
    TableStyle,
    TemplateTable,
    build_row,
    gap_row,
    header_mismatches,
    row_cells,
    source_row,
    source_shaped_table,
    table_rows,
)
from .xml import (
    W_P,
    W_SDT,
    W_SDT_CONTENT,
    W_TBL,
    W_TC,
    W_TR,
    add_bookmark,
    ancestor,
    base_ppr,
    base_rpr,
    cell_container,
    clear_blocks,
    ensure_ends_with_paragraph,
    make_plain,
    next_bookmark_id,
    norm,
    ref_field,
    remove,
    sdt_tag,
    text_of,
    unwrap,
    w_el,
)
from .toc import rebuild_toc
from .verify import verify_rendered

SLOT_TAG_PREFIX = "CC_"


class RenderError(RuntimeError):
    """The template file cannot be rendered into (missing or unreadable)."""


class RenderBlocked(RuntimeError):
    """A final export was asked for while required-slot gaps are still unresolved."""

    def __init__(self, slot_ids: list[str]):
        super().__init__(f"unresolved gaps: {', '.join(slot_ids)}")
        self.slot_ids = slot_ids


def default_ref_text(source: SourceDocument) -> RefText:
    """Show a reference token as the source wrote it ("see section 6.2") until Phase 12 renumbers it."""
    phrases = raw_text_by_target(source)

    def text(target: str, claim: Claim) -> str:
        return next((phrases[(u, target)] for u in claim.source_unit_ids if (u, target) in phrases), target)

    return text


def assembled_ref_text(assembled: AssembledDocument, fallback: RefText, fields: bool = True) -> RefText:
    """A token shows its resolved phrase; the new number in it is a REF field to the target's bookmark (plain text
    when *fields* is off: a template whose headings Word does not number)."""

    def text(target: str, claim: Claim) -> str:
        ref = assembled.ref(claim.claim_id, target)
        if ref is None:
            return fallback(target, claim)
        if not fields or ref.number_at is None or not ref.bookmark or not ref.number:
            return ref.text
        end = ref.number_at + len(ref.number)
        return ref.text[:ref.number_at] + ref_field(ref.bookmark, ref.number) + ref.text[end:]

    return text


def headings_numbered(template_file: Path) -> bool:
    """True when Word numbers the template's chapter headings (a numbered heading style), so REF fields work."""
    try:
        doc = Document(str(template_file))
    except Exception:
        return False
    resolver = NumberingResolver(doc)
    headings = [p for p in doc.element.body.iter(W_P) if is_heading(doc, p) and text_of(p)]
    return any(resolver.effective_numpr(p) is not None for p in headings)


def gap_slots(drafts: Iterable[SectionDraft]) -> list[str]:
    return [s.slot_id for d in drafts for s in d.slots if any(c.is_gap_marker for c in s.claims)]


def render_document(
    template: TemplateModel,
    drafts: list[SectionDraft],
    source: SourceDocument,
    out_path: str | Path,
    *,
    job_id: str,
    version: int = 1,
    slot_plan: Optional[SlotPlan] = None,
    mode: RenderMode = RenderMode.REVIEW,
    accepted_gaps: Iterable[str] = (),
    ref_text: Optional[RefText] = None,
    page_counter: Optional[PageCounter] = None,
    assembled: Optional[AssembledDocument] = None,
    number_map: Optional[NumberMap] = None,
) -> RenderReport:
    """Render *drafts* into a copy of the template's normalized file at *out_path*, check it, and report.

    *page_counter* (e.g. ``pages.word_page_counter()``) measures the page of each table-of-contents entry on the
    written file; the entries then carry their page numbers and Word is not asked to update fields on opening.
    *assembled* and *number_map* (Phase 12) give the resolved references and the bookmarks they point to.
    """
    accepted = set(accepted_gaps)
    if mode == RenderMode.FINAL:
        open_gaps = [s for s in gap_slots(drafts) if s not in accepted]
        if open_gaps:
            raise RenderBlocked(open_gaps)
    template_file = Path(template.normalized_file or "")
    if not template.normalized_file or not template_file.exists():
        raise RenderError(f"normalized template file not found: {template.normalized_file!r}")
    numbered = number_map is not None and headings_numbered(template_file)
    if ref_text is None:
        ref_text = (assembled_ref_text(assembled, default_ref_text(source), fields=numbered) if assembled
                    else default_ref_text(source))
    renderer = _Renderer(template, source, slot_plan, mode, accepted, ref_text, template_file,
                         number_map if numbered else None)
    if number_map is not None and not numbered and assembled is not None and assembled.refs:
        renderer.warnings.append("the template's headings are not numbered by Word: cross-references show the new "
                                 "chapter numbers as plain text, not as REF fields")
    renderer.run(drafts)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    renderer.doc.save(str(out_path))
    toc_pages = False
    if renderer.toc and page_counter is not None:
        pages = page_counter(out_path, [e.bookmark for e in renderer.toc]) or {}
        if all(e.bookmark in pages for e in renderer.toc):
            renderer.toc = rebuild_toc(renderer.doc, pages)
            renderer.update_fields_on_open(False)
            renderer.doc.save(str(out_path))
            toc_pages = True
        else:
            renderer.warnings.append("table of contents: Word could not measure the page numbers; "
                                     "Word fills them in when the document's fields are updated")
    report = RenderReport(
        job_id=job_id, version=version, mode=mode, template_file=str(template_file),
        draft_versions={d.target_section_id: d.version for d in drafts},
        slots=renderer.slot_renders, regions=renderer.regions, sections_removed=renderer.sections_removed,
        warnings=list(dict.fromkeys(renderer.warnings + renderer.ctx.warnings)),
        toc_entries=len(renderer.toc or []), toc_page_numbers=toc_pages,
    )
    report.problems = verify_rendered(out_path, template_file, template, drafts, source, report, ref_text,
                                      number_map if numbered else None)
    return report


# ── Anchors ───────────────────────────────────────────────────────────


@dataclass
class _Target:
    kind: str  # "container" | "rows" | "paragraph"
    container: object = None             # where blocks go (sdtContent, table cell)
    control: object = None               # the content control around the container, if any
    table: object = None                 # rows: the template table
    header_rows: list = field(default_factory=list)
    example_rows: list = field(default_factory=list)
    paragraph: object = None             # paragraph: the bookmark or placeholder paragraph


class _Renderer:
    def __init__(self, template: TemplateModel, source: SourceDocument, slot_plan: Optional[SlotPlan],
                 mode: RenderMode, accepted: set[str], ref_text: RefText, template_file: Path,
                 number_map: Optional[NumberMap] = None):
        try:
            self.doc = Document(str(template_file))
        except Exception as exc:
            raise RenderError(f"cannot open the template file {template_file}: {exc}") from exc
        self.template, self.source, self.slot_plan, self.number_map = template, source, slot_plan, number_map
        self.mode, self.accepted = mode, accepted
        self.body = self.doc.element.body
        self.colour = TextColour(self.doc)
        self.tables = [el for el in iter_body_blocks(self.body) if el.tag == W_TBL]
        self.controls: dict[str, list] = defaultdict(list)
        for sdt in self.body.iter(W_SDT):
            tag = sdt_tag(sdt)
            if tag:
                self.controls[tag].append(sdt)
        palette = palette_from_document(self.doc)
        self.legend = self.tables[palette.legend_table_index] if palette.legend_table_index is not None else None
        self.callouts = Callouts(template.callout_palette, self.tables)
        self.ctx = ContentContext(
            doc=self.doc, numbering=ListNumbering(self.doc), callouts=self.callouts,
            units={u.unit_id: u for u in source.iter_units()}, ref_text=ref_text,
            table_style=self._table_style(), max_image_emu=self._text_width_emu(),
            bookmarks={e.claim_id: e.bookmark for e in (number_map.entries if number_map else [])
                       if e.kind == NumberKind.HEADING and e.exact and e.claim_id and e.bookmark},
            bookmark_id=next_bookmark_id(self.body),
        )
        self.keep: set = set()
        self.slot_renders: list[SlotRender] = []
        self.regions = []
        self.sections_removed: list[str] = []
        self.warnings: list[str] = []
        self.toc = None

    # ── Run ───────────────────────────────────────────────────────────

    def run(self, drafts: list[SectionDraft]) -> None:
        self.regions = settle_regions(self.template, self.slot_plan, self.controls)
        by_section = {d.target_section_id: d for d in drafts}
        headings = self._section_headings()
        for section in sorted(self.template.sections, key=lambda s: s.display_order):
            draft = by_section.get(section.section_id)
            slots = {s.slot_id: s for s in draft.slots} if draft else {}
            self.ctx.section_level = section.level
            for slot in sorted(section.slots, key=lambda s: s.display_order):
                slot_draft = slots.get(slot.slot_id)
                self.slot_renders.append(self._slot(section, slot, slot_draft.claims if slot_draft else []))
            self._optional_section(section, headings.get(section.section_id))
        self._bookmark_chapters(headings)
        if self.legend is not None and self.legend.getparent() is not None:
            remove(self.legend)
        self._retain_instructions()
        remove_instructions(self.doc, self.colour, self.keep)
        if self.mode == RenderMode.FINAL:
            for sdt in list(self.body.iter(W_SDT)):
                if (sdt_tag(sdt) or "").startswith(SLOT_TAG_PREFIX):
                    unwrap(sdt)
        self._tidy()
        self._renumber_drawings()
        self.toc = rebuild_toc(self.doc)
        self.update_fields_on_open(True)
        bad = sum("�" in c.text for d in drafts for c in d.iter_claims())
        if bad:
            self.warnings.append(f"{bad} claims contain the replacement character '�' (broken quotes or "
                                 "apostrophes in the source file)")

    # ── Slots ─────────────────────────────────────────────────────────

    def _slot(self, section: TargetSection, slot: TargetSlot, claims: list[Claim]) -> SlotRender:
        target = self._locate(slot)
        content = [c for c in claims if not c.is_gap_marker]
        gap = any(c.is_gap_marker for c in claims)
        if target is None:
            note = "anchor not found in the template file"
            if content or gap:
                self.warnings.append(f"{slot.slot_id}: {note}; {len(content)} claims not rendered")
            return SlotRender(slot_id=slot.slot_id, outcome=SlotOutcome.NO_ANCHOR, claims=len(content), note=note)
        if not content:
            if gap and self.mode == RenderMode.REVIEW:
                self._write(slot, target, [], gap=True)
                return SlotRender(slot_id=slot.slot_id, outcome=SlotOutcome.GAP_MARKER)
            self._remove_target(target)
            if gap:
                return SlotRender(slot_id=slot.slot_id, outcome=SlotOutcome.REMOVED_ACCEPTED_GAP,
                                  note="accepted as N/A by the reviewer")
            if slot.required and not any(s.slot_id == slot.slot_id for s in self._one_of_filled(section)):
                self.warnings.append(f"{slot.slot_id}: required slot without content or gap marker; removed")
            return SlotRender(slot_id=slot.slot_id, outcome=SlotOutcome.REMOVED_EMPTY)
        boxes, tables = self.ctx.callout_boxes, self.ctx.tables
        self._write(slot, target, content)
        return SlotRender(slot_id=slot.slot_id, outcome=SlotOutcome.FILLED, claims=len(content),
                          tables=self.ctx.tables - tables, callout_boxes=self.ctx.callout_boxes - boxes)

    @staticmethod
    def _one_of_filled(section: TargetSection) -> list[TargetSlot]:
        """Slots a one-of group lets stay empty (any member may be the filled one)."""
        keys = {k for group in section.one_of for k in group}
        return [s for s in section.slots if s.key in keys]

    def _locate(self, slot: TargetSlot) -> Optional[_Target]:
        anchor = slot.anchor
        if anchor is None:
            return None
        if anchor.kind == AnchorKind.CONTENT_CONTROL:
            sdts = [s for s in self.controls.get(anchor.ref, []) if s.getparent() is not None]
            if not sdts:
                return None
            if any(s.find(W_SDT_CONTENT) is not None and s.find(W_SDT_CONTENT).find(qn("w:tr")) is not None for s in sdts):
                tbl = ancestor(sdts[0], W_TBL)
                rows = table_rows(tbl)
                example = [tr for s in sdts for tr in s.find(W_SDT_CONTENT).findall(qn("w:tr"))]
                header = [tr for tr in rows[:rows.index(example[0])]]
                return _Target("rows", table=tbl, header_rows=header, example_rows=example)
            for extra in sdts[1:]:
                remove(extra)
            return _Target("container", container=sdts[0].find(W_SDT_CONTENT), control=sdts[0])
        if anchor.kind == AnchorKind.TABLE_CELL:
            if anchor.table_index is None or anchor.table_index >= len(self.tables):
                return None
            tbl = self.tables[anchor.table_index]
            rows = table_rows(tbl)
            if anchor.row_index >= len(rows):
                return None
            if slot.formatting_profile == FormattingProfile.TABLE:
                return _Target("rows", table=tbl, header_rows=rows[:anchor.row_index], example_rows=rows[anchor.row_index:])
            cells = row_cells(rows[anchor.row_index])
            if anchor.column_index >= len(cells):
                return None
            return _Target("container", container=cell_container(cells[anchor.column_index]))
        if anchor.kind == AnchorKind.BOOKMARK:
            mark = next((b for b in self.body.iter(qn("w:bookmarkStart")) if b.get(qn("w:name")) == anchor.ref), None)
            p = ancestor(mark, W_P) if mark is not None else None
            return _Target("paragraph", paragraph=p) if p is not None else None
        if anchor.kind == AnchorKind.PLACEHOLDER:
            p = next((p for p in self.body.iter(W_P) if anchor.ref in "".join(t.text or "" for t in p.iter(qn("w:t")))), None)
            return _Target("paragraph", paragraph=p) if p is not None else None
        return None  # a proximity anchor is never ready: the template must be normalized first

    def _write(self, slot: TargetSlot, target: _Target, claims: list[Claim], gap: bool = False) -> None:
        if target.kind == "rows":
            self._write_rows(slot, target, claims, gap)
            return
        if target.kind == "paragraph":
            p = target.paragraph
            ppr, rpr = base_ppr(p), base_rpr(p)
            blocks = [gap_marker(ppr)] if gap else build_blocks(claims, self.ctx, ppr, rpr, own_callout=slot.callout_kind)
            for block in blocks:
                p.addprevious(block)
            self._keep(blocks)
            if slot.instruction_behavior == InstructionBehavior.RETAIN_AS_LABEL:
                make_plain(p)
                self._keep([p])
            else:
                _move_bookmarks(p, blocks)
                remove(p)
            return
        container = target.container
        in_cell = container.tag == W_TC or ancestor(container, W_TC) is not None
        first = container.find(W_P) if container.find(W_P) is not None else next(container.iter(W_P), None)
        ppr, rpr = base_ppr(first), base_rpr(container)
        if ppr is not None and not in_cell:
            # Body text keeps the instruction's alignment but the template's own paragraph spacing.
            for node in [c for c in ppr if c.tag in (qn("w:spacing"), qn("w:ind"))]:
                ppr.remove(node)
        blocks = [gap_marker(ppr)] if gap else build_blocks(claims, self.ctx, ppr, rpr, own_callout=slot.callout_kind)
        if slot.instruction_behavior == InstructionBehavior.RETAIN_AS_LABEL:
            make_plain(container)
            self._keep(list(container))
        else:
            clear_blocks(container)
        for block in blocks:
            container.append(block)
        self._keep(blocks)
        if in_cell:
            ensure_ends_with_paragraph(container)

    def _write_rows(self, slot: TargetSlot, target: _Target, claims: list[Claim], gap: bool) -> None:
        template_table = TemplateTable(target.table, target.header_rows, target.example_rows)
        pristine = copy.deepcopy(target.table)
        anchor_el = self._top_row_element(target.example_rows[0]) if target.example_rows else None
        if gap:
            self._replace_rows(target.table, target.example_rows, [gap_row(template_table.prototype, gap_marker())], anchor_el)
            self._keep([target.table])
            return
        # Claim order is kept: lead-in text before its table, rows of one source table together.
        segments: list[tuple[str, list]] = []
        for claim in claims:
            unit = self.ctx.units.get(claim.source_unit_ids[0]) if claim.source_unit_ids else None
            if claim.kind == ClaimKind.TABLE_ROW and unit is not None and unit.table_ref is not None:
                row = source_row(claim, unit, self.ctx.warnings)
                if segments and segments[-1][0] == "rows" and segments[-1][1][-1].table_id == row.table_id:
                    segments[-1][1].append(row)
                else:
                    segments.append(("rows", [row]))
            elif segments and segments[-1][0] == "text":
                segments[-1][1].append(claim)
            else:
                segments.append(("text", [claim]))
        written: list = []
        cursor = None  # the element the next block follows; None: before the template table

        def place(el) -> None:
            nonlocal cursor
            if cursor is not None and cursor.tag == W_TBL and el.tag == W_TBL:
                place(w_el("p"))  # Word merges two tables that touch
            if cursor is None:
                target.table.addprevious(el)
            else:
                cursor.addnext(el)
            cursor = el
            written.append(el)

        placed_template = False
        for kind, items in segments:
            if kind == "text":
                for block in build_blocks(items, self.ctx):
                    place(block)
                continue
            if template_table.fits(items[0].header):
                new_rows = [build_row(template_table.prototype, r.cells) for r in items]
                if not placed_template:
                    self._replace_rows(target.table, target.example_rows, new_rows, anchor_el)
                    if cursor is not None and cursor.tag == W_TBL:
                        target.table.addprevious(w_el("p"))
                    cursor = target.table
                    written.append(target.table)
                    placed_template = True
                else:
                    tbl = copy.deepcopy(pristine)
                    copy_rows = table_rows(tbl)
                    examples = copy_rows[len(target.header_rows):]
                    self._replace_rows(tbl, examples, new_rows, self._top_row_element(examples[0]) if examples else None)
                    place(tbl)
                for problem in header_mismatches(template_table, items[0].header):
                    self.warnings.append(f"{slot.slot_id}: {problem} (table {items[0].table_id}); check the column")
            else:
                place(source_shaped_table(TableStyle.from_template(template_table), items))
                self.warnings.append(
                    f"{slot.slot_id}: source table {items[0].table_id} ({' | '.join(items[0].header)}) does not fit the "
                    f"template table ({' | '.join(template_table.header_texts)}); written with its own columns")
            self.ctx.tables += 1
        if not placed_template:
            remove(target.table)  # no source table fits it: each was written in its own shape
        self._keep(written)

    @staticmethod
    def _top_row_element(tr):
        """A row, or the row-level content control around it."""
        parent = tr.getparent()
        return parent.getparent() if parent is not None and parent.tag == W_SDT_CONTENT else tr

    def _replace_rows(self, tbl, example_rows: list, new_rows: list, anchor_el) -> None:
        if anchor_el is not None and anchor_el.getparent() is tbl:
            for row in new_rows:
                anchor_el.addprevious(row)
        else:
            for row in new_rows:
                tbl.append(row)
        for tr in example_rows:
            top = self._top_row_element(tr)
            if top.getparent() is not None:
                remove(top)

    def _remove_target(self, target: _Target) -> None:
        if target.kind == "rows":
            self._remove_block(target.table)
        elif target.kind == "paragraph":
            self._remove_block(target.paragraph)
        else:
            cell = target.container if target.container.tag == W_TC else ancestor(target.container, W_TC)
            if cell is None:
                self._remove_block(target.control if target.control is not None else target.container)
                return
            tr = ancestor(cell, W_TR)
            tbl = ancestor(tr, W_TBL)
            remove(self._top_row_element(tr))
            if not table_rows(tbl):
                self._remove_block(tbl)

    @staticmethod
    def _remove_block(el) -> None:
        parent = el.getparent()
        if parent is None:
            return
        remove(el)
        if parent.tag == W_TC:
            ensure_ends_with_paragraph(parent)

    def _keep(self, blocks: list) -> None:
        for block in blocks:
            self.keep.update(block.iter())

    # ── Sections ──────────────────────────────────────────────────────

    def _section_headings(self) -> dict[str, object]:
        """Template section → its heading paragraph, matched in order by heading text."""
        paragraphs = [el for el in iter_body_blocks(self.body) if el.tag == W_P and is_heading(self.doc, el) and text_of(el)]
        out, cursor = {}, 0
        for section in sorted(self.template.sections, key=lambda s: s.display_order):
            want = norm(_strip_optional(section.heading))
            for i in range(cursor, len(paragraphs)):
                if norm(_strip_optional(text_of(paragraphs[i]))) == want:
                    out[section.section_id] = paragraphs[i]
                    cursor = i + 1
                    break
            else:
                self.warnings.append(f"heading of section {section.section_id} ('{section.heading}') not found")
        return out

    def _bookmark_chapters(self, headings: dict[str, object]) -> None:
        """Each chapter heading still in the document gets its NumberMap bookmark (the target of REF fields)."""
        for entry in (self.number_map.entries if self.number_map else []):
            heading = headings.get(entry.source_id)
            if entry.kind != NumberKind.SECTION or heading is None or heading.getparent() is None or not entry.bookmark:
                continue
            add_bookmark(heading, entry.bookmark, self.ctx.bookmark_id)
            self.ctx.bookmark_id += 1

    def _optional_section(self, section: TargetSection, heading) -> None:
        if not section.optional_marker or heading is None:
            return
        filled = {r.slot_id for r in self.slot_renders
                  if r.outcome in (SlotOutcome.FILLED, SlotOutcome.GAP_MARKER) and r.slot_id in {s.slot_id for s in section.slots}}
        if filled:
            for t in heading.iter(qn("w:t")):
                t.text = re.sub(r"\s*\(optional\)", "", t.text or "", flags=re.I)
            for node in list(heading.iter(qn("w:highlight"))):
                node.getparent().remove(node)
            return
        top = heading
        while top.getparent() is not self.body:
            top = top.getparent()
        span = [top]
        for sibling in top.itersiblings():
            if sibling.tag == qn("w:sectPr") or self._starts_section(sibling, section.level):
                break
            span.append(sibling)
        for el in span:
            if el.tag == W_P and el.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is not None:
                for child in [c for c in el if c.tag != qn("w:pPr")]:
                    el.remove(child)
                continue
            remove(el)
        self.sections_removed.append(section.section_id)

    def _starts_section(self, el, level: int) -> bool:
        paragraphs = [el] if el.tag == W_P else [p for p in iter_body_blocks(el) if p.tag == W_P] if el.tag == W_SDT else []
        for p in paragraphs:
            if is_heading(self.doc, p) and text_of(p):
                match = re.search(r"(\d)", style_name(self.doc, p))
                return int(match.group(1)) <= level if match else True
        return False

    # ── Instructions kept on purpose ──────────────────────────────────

    def _retain_instructions(self) -> None:
        """``retain_separate``: the slot's instruction paragraphs stay (plain) beside the content."""
        for slot in self.template.iter_slots():
            if slot.instruction_behavior != InstructionBehavior.RETAIN_SEPARATE:
                continue
            lines = {norm(line) for line in slot.instruction.split("\n") if line.strip()}
            for p in self.body.iter(W_P):
                if p not in self.keep and norm(text_of(p)) in lines:
                    make_plain(p)
                    self.keep.add(p)

    # ── Finishing ─────────────────────────────────────────────────────

    def _tidy(self) -> None:
        """Collapse runs of empty body paragraphs; keep an empty paragraph between tables that touch."""
        for parent in [self.body] + list(self.body.iter(W_SDT_CONTENT)):
            previous_empty = False
            for child in list(parent):
                empty = (child.tag == W_P and child.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is None
                         and not text_of(child) and next(child.iter(qn("w:drawing")), None) is None
                         and next(child.iter(qn("w:fldChar")), None) is None)
                if empty and previous_empty and parent is self.body:
                    remove(child)
                    continue
                previous_empty = empty
            children = list(parent)
            for a, b in zip(children, children[1:]):
                if a.tag == W_TBL and b.tag == W_TBL:
                    a.addnext(w_el("p"))

    def _renumber_drawings(self) -> None:
        """Clones and pictures must not share a drawing ID; numbering starts above the headers' and footers'."""
        used = []
        for rel in self.doc.part.rels.values():
            part = rel.target_part if not rel.is_external else None
            if part is not None and part is not self.doc.part and hasattr(part, "element"):
                used += [int(d.get("id")) for d in part.element.iter(qn("wp:docPr")) if (d.get("id") or "").isdigit()]
        next_id = max(used, default=0) + 1
        for node in self.body.iter(qn("wp:docPr")):
            node.set("id", str(next_id))
            next_id += 1

    def update_fields_on_open(self, on: bool) -> None:
        """Word refreshes the table of contents' page numbers (and later the REF fields) when the document is opened."""
        settings = self.doc.settings.element
        for node in settings.findall(qn("w:updateFields")):
            settings.remove(node)
        if not on:
            return
        node = w_el("updateFields", val="true")
        after = {qn(f"w:{t}") for t in ("hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars", "rsids")}
        follower = next((c for c in settings if c.tag in after or c.tag.endswith("}mathPr") or c.tag.endswith("}themeFontLang")
                         or c.tag.endswith("}clrSchemeMapping") or c.tag.endswith("}shapeDefaults")
                         or c.tag.endswith("}decimalSymbol") or c.tag.endswith("}listSeparator")), None)
        if follower is not None:
            follower.addprevious(node)
        else:
            settings.append(node)

    # ── Setup helpers ─────────────────────────────────────────────────

    def _table_style(self) -> TableStyle:
        """Formatting for tables written in the source's shape: the template's first data table."""
        for slot in self.template.iter_slots():
            if slot.formatting_profile != FormattingProfile.TABLE or slot.anchor is None:
                continue
            target = self._locate(slot)
            if target is not None and target.kind == "rows" and target.example_rows:
                tbl = copy.deepcopy(target.table)
                rows = table_rows(tbl)
                n = len(target.header_rows)
                return TableStyle.from_template(TemplateTable(tbl, rows[:n], rows[n:]))
        return TableStyle.plain()

    def _text_width_emu(self) -> int:
        section = self.doc.sections[0]
        try:
            return int(section.page_width - section.left_margin - section.right_margin)
        except TypeError:
            return 5_486_400  # 6 inches


def _move_bookmarks(p, blocks: list) -> None:
    """A replaced paragraph's bookmarks move around the new content (start in the first paragraph, end in the last)."""
    paragraphs = [b for b in blocks if b.tag == W_P]
    if not paragraphs:
        return
    for mark in list(p.iter(qn("w:bookmarkStart"))):
        ppr = paragraphs[0].find(qn("w:pPr"))
        (ppr.addnext(mark) if ppr is not None else paragraphs[0].insert(0, mark))
    for mark in list(p.iter(qn("w:bookmarkEnd"))):
        paragraphs[-1].append(mark)


def _strip_optional(text: str) -> str:
    return re.sub(r"\s*\(optional\)\s*", " ", text or "", flags=re.I)

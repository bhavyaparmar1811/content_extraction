"""Post-render check: open the written ``.docx`` and confirm it is what the drafts and the report say (Phase 11).

Each failure is a problem on the ``RenderReport``; a job whose document has a
problem does not complete cleanly. Checked:
- every claim of a filled slot is in the document (table rows cell by cell;
  a figure by its image);
- gap markers: one per gap slot in the review draft, none in the final export;
- no blue instruction text is left in the body;
- no conditional-region control is left, and in the final export no slot control;
- every list paragraph points at a numbering definition that exists;
- drawing IDs are unique;
- the table of contents lists the document's headings, and its links have targets;
- each bookmarked chapter and heading has the number the ``NumberMap`` gives it, and every REF field has its
  bookmark (Phase 12);
- headers and footers are byte-for-byte the template's.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn

from app.schemas.v2 import (
    ClaimKind,
    NumberKind,
    NumberMap,
    RenderMode,
    RenderReport,
    SectionDraft,
    SlotOutcome,
    SourceDocument,
    TemplateModel,
)
from app.services.parser.ooxml import NumberingResolver, is_toc_sdt

from ..template.callout_palette import style_name
from ..template.colour import TextColour
from .content import GAP_MARKER_TEXT, RefText, show_refs
from .toc import toc_problems
from .xml import W_P, W_SDT, ancestor, plain_text, sdt_tag

_PROBE = 60  # characters of each claim looked for in the document


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _header_footer_xml(doc) -> dict[str, bytes]:
    out = {}
    for part in doc.part.package.iter_parts():
        name = str(part.partname)
        if re.match(r"/word/(header|footer)\d*\.xml$", name):
            out[name] = part.blob
    return out


def verify_rendered(path: Path, template_file: Path, template: TemplateModel, drafts: list[SectionDraft],
                    source: SourceDocument, report: RenderReport, ref_text: RefText,
                    number_map: Optional[NumberMap] = None) -> list[str]:
    problems: list[str] = []
    try:
        doc = Document(str(path))
    except Exception as exc:
        return [f"the rendered document cannot be opened: {exc}"]
    body = doc.element.body
    paragraphs = list(body.iter(W_P))
    text = _squash(" ".join("".join(t.text or "" for t in p.iter(qn("w:t"))) for p in paragraphs))

    filled = {s.slot_id for s in report.slots if s.outcome == SlotOutcome.FILLED}
    units = {u.unit_id: u for u in source.iter_units()}
    missing: list[str] = []
    figures: list[tuple[str, str]] = []  # (claim, image file) of figures that should be in the document
    for draft in drafts:
        for slot in draft.slots:
            if slot.slot_id not in filled:
                continue
            for claim in slot.claims:
                if claim.is_gap_marker:
                    continue
                if claim.kind == ClaimKind.FIGURE:
                    unit = units.get(claim.source_unit_ids[0]) if claim.source_unit_ids else None
                    asset = next((a for a in (unit.assets if unit else []) if a.kind.value == "figure"), None)
                    if asset is not None and asset.path and Path(asset.path).exists():
                        figures.append((f"{slot.slot_id}/{claim.claim_id}", asset.path))
                    continue
                shown = plain_text(show_refs(claim, ref_text))
                if claim.kind == ClaimKind.TABLE_ROW:
                    unit = units.get(claim.source_unit_ids[0]) if claim.source_unit_ids else None
                    edited = unit is None or claim.text.strip() != unit.text.strip()
                    probes = ([p for p in shown.split("|")] if edited
                              else [c.text for c in unit.table_ref.cells] if unit.table_ref else [shown])
                else:
                    probes = [shown]
                for probe in probes:
                    probe = _squash(probe)[:_PROBE]
                    if probe and probe not in text:
                        missing.append(f"{slot.slot_id}/{claim.claim_id}")
                        break
    if missing:
        problems.append(f"{len(missing)} claims not found in the document: {', '.join(missing[:8])}")
    placed = _embedded_image_hashes(doc)
    lost = [claim for claim, path in figures if hashlib.sha1(Path(path).read_bytes()).hexdigest() not in placed]
    if lost:
        problems.append(f"{len(lost)} figures are not in the document: {', '.join(lost[:8])}")

    markers = text.count(_squash(GAP_MARKER_TEXT))
    expected = sum(s.outcome == SlotOutcome.GAP_MARKER for s in report.slots)
    if report.mode == RenderMode.FINAL and markers:
        problems.append(f"the final document still shows {markers} gap markers")
    elif markers != expected:
        problems.append(f"{markers} gap markers in the document, {expected} gap slots rendered")

    colour = TextColour(doc)
    blue = [p for p in paragraphs if not _in_toc(p) and not _styled_structure(doc, p)
            and colour.classify(p) in ("blue", "mixed") and _has_blue_run(colour, p)]
    if blue:
        sample = "; ".join(repr("".join(t.text or "" for t in p.iter(qn("w:t")))[:50]) for p in blue[:3])
        problems.append(f"{len(blue)} paragraphs of blue instruction text left: {sample}")

    tags = [sdt_tag(s) or "" for s in body.iter(W_SDT)]
    if any(t.startswith("COND_") for t in tags):
        problems.append("conditional-region content controls are left in the document")
    if report.mode == RenderMode.FINAL and any(t.startswith("CC_") for t in tags):
        problems.append("slot content controls are left in the final document")

    known = {n.get(qn("w:numId")) for n in doc.part.numbering_part.element.findall(qn("w:num"))}
    dangling = {n.get(qn("w:val")) for n in body.iter(qn("w:numId"))} - known - {"0"}
    if dangling:
        problems.append(f"list paragraphs use numbering that does not exist: {sorted(dangling)}")

    ids = [d.get("id") for d in body.iter(qn("wp:docPr"))]
    if len(ids) != len(set(ids)):
        problems.append("drawing IDs are not unique")
    problems += toc_problems(doc)
    if number_map is not None:
        problems += number_problems(doc, number_map)

    try:
        before = _header_footer_xml(Document(str(template_file)))
        after = _header_footer_xml(doc)
        if before != after:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            problems.append(f"headers or footers differ from the template: {changed}")
    except Exception as exc:  # the template file is checked before rendering; this is defensive
        problems.append(f"headers and footers could not be compared: {exc}")
    return problems


def number_problems(doc, number_map: NumberMap) -> list[str]:
    """Word's number of each bookmarked chapter and heading is the NumberMap's; every REF field has its bookmark."""
    body = doc.element.body
    resolver = NumberingResolver(doc)
    numbers = {}
    for p in body.iter(W_P):
        info = resolver.advance(p)  # every paragraph counts, as in Word
        if info is not None and not info.is_bullet:
            numbers[p] = info.number
    marks = {}
    for mark in body.iter(qn("w:bookmarkStart")):
        p = ancestor(mark, W_P)
        if p is not None:
            marks.setdefault(mark.get(qn("w:name")), p)
    problems, seen = [], set()
    for entry in number_map.entries:
        if entry.kind == NumberKind.UNIT or not entry.exact or not entry.bookmark or entry.bookmark in seen:
            continue
        seen.add(entry.bookmark)
        p = marks.get(entry.bookmark)
        if p is None:
            problems.append(f"{entry.source_id} ({entry.target_number}) has no bookmark {entry.bookmark} in the document")
        elif numbers.get(p) != entry.target_number:
            problems.append(f"{entry.source_id}: Word numbers it {numbers.get(p)}, the number map says {entry.target_number}")
    for instr in body.iter(qn("w:instrText")):
        m = re.match(r"\s*REF\s+(\S+)", instr.text or "")
        if m and m.group(1) not in marks:
            problems.append(f"a REF field points to the missing bookmark {m.group(1)}")
    return problems


def _embedded_image_hashes(doc) -> set[str]:
    """SHA-1 of every image the body shows."""
    out = set()
    for blip in doc.element.body.iter(qn("a:blip")):
        part = doc.part.related_parts.get(blip.get(qn("r:embed")))
        if part is not None and hasattr(part, "blob"):
            out.add(hashlib.sha1(part.blob).hexdigest())
    return out


def _styled_structure(doc, p) -> bool:
    """Headings and captions take their colour from their style; that colour is the template's design, not an
    instruction."""
    return style_name(doc, p).lower().startswith(("heading", "title", "caption", "toc"))


def _has_blue_run(colour: TextColour, p) -> bool:
    style = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    p_style = style.get(qn("w:val")) if style is not None else None
    return any("".join(t.text or "" for t in r.iter(qn("w:t"))).strip() and colour.run_is_blue(r, p_style)
               for r in p.iter(qn("w:r")))


def _in_toc(el) -> bool:
    parent = ancestor(el, W_SDT)
    while parent is not None:
        if is_toc_sdt(parent):
            return True
        parent = ancestor(parent, W_SDT)
    return False

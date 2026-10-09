"""Table of contents: the TOC field's entries rebuilt from the rendered headings.

The template's TOC field holds the template's own entries (its sections, its
page numbers). Word replaces them only when it updates fields, so a viewer
that does not, or a "No" to Word's "update fields?" question, shows the
template's old list. ``rebuild_toc`` writes the entries itself:

- one entry per heading the field's switches include (``\\t`` style list,
  else ``\\o`` heading levels, else Heading 1-3), in document order;
- the Word number of the heading ("6.2.1"), its text, a hyperlink and a
  ``PAGEREF`` field to a bookmark on the heading (added when it has none);
- the page number the caller measured (``pages``, from Word's layout), else
  empty: Word fills it in when it updates fields.

The field itself stays, so Word can still refresh the whole table. Running it
again on its own output gives the same entries (the bookmarks are reused).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Optional

from docx.oxml.ns import qn

from app.services.parser.ooxml import NumberingResolver, is_toc_sdt

from ..template.callout_palette import style_name
from .xml import W_P, W_PPR, W_RPR, W_SDT, ancestor, set_ppr_child, text_of, w_el

DEFAULT_LEVELS = {"heading 1": 1, "heading 2": 2, "heading 3": 3}
_FIRST_INDENT = 567   # twips from the entry's number to its text at level 1 (1 cm)
_LEVEL_STEP = 284     # each deeper level starts 0.5 cm further in, with 0.5 cm more room for its number
_W_INSTR, _W_FLD = qn("w:instrText"), qn("w:fldChar")


@dataclass
class TocEntry:
    level: int
    number: Optional[str]
    title: str
    bookmark: str
    page: Optional[int] = None


def toc_levels(instruction: str) -> dict[str, int]:
    """Style name (lower case) → TOC level, from the field's ``\\t`` and ``\\o`` switches."""
    levels: dict[str, int] = {}
    styles = re.search(r'\\t\s+"([^"]*)"', instruction)
    if styles:
        parts = [p.strip() for p in styles.group(1).split(",")]
        for name, level in zip(parts[0::2], parts[1::2]):
            if name and level.isdigit():
                levels[name.lower()] = int(level)
    outline = re.search(r'\\o\s+"(\d)-(\d)"', instruction)
    if outline:
        for n in range(int(outline.group(1)), int(outline.group(2)) + 1):
            levels.setdefault(f"heading {n}", n)
    return levels or dict(DEFAULT_LEVELS)


def _toc_start(body):
    """The instrText run of the first TOC field in the body, or None."""
    return next((i for i in body.iter(_W_INSTR) if (i.text or "").strip().upper().startswith("TOC")), None)


def _field_paragraphs(first_p) -> list:
    """The paragraphs from the one holding the TOC field's begin to the one holding its end."""
    out, depth, p = [], 0, first_p
    while p is not None:
        if p.tag == W_P:
            out.append(p)
            for fld in p.iter(_W_FLD):
                kind = fld.get(qn("w:fldCharType"))
                depth += kind == "begin"
                depth -= kind == "end"
            if depth <= 0:
                return out
        p = p.getnext()
    return out


def _in_toc(el, toc_paragraphs: set) -> bool:
    p = el if el.tag == W_P else ancestor(el, W_P)
    if p in toc_paragraphs:
        return True
    node = el.getparent()
    while node is not None:
        if node.tag == W_SDT and is_toc_sdt(node):
            return True
        node = node.getparent()
    return False


def _bookmark(p, next_id: list[int], used: set[str]) -> str:
    for mark in p.iter(qn("w:bookmarkStart")):
        name = mark.get(qn("w:name")) or ""
        if name and name != "_GoBack":
            return name
    n = 1
    while f"_Toc{n:09d}" in used:
        n += 1
    name = f"_Toc{n:09d}"
    used.add(name)
    start = w_el("bookmarkStart", id=next_id[0], name=name)
    end = w_el("bookmarkEnd", id=next_id[0])
    next_id[0] += 1
    ppr = p.find(W_PPR)
    (ppr.addnext(start) if ppr is not None else p.insert(0, start))
    p.append(end)
    return name


def heading_entries(doc, levels: dict[str, int], toc_paragraphs: set = frozenset()) -> list[TocEntry]:
    """The headings a TOC with *levels* lists, in document order, each with a bookmark (added if missing)."""
    body = doc.element.body
    marks = list(body.iter(qn("w:bookmarkStart")))
    used = {m.get(qn("w:name")) for m in marks}
    next_id = [max((int(m.get(qn("w:id"))) for m in marks if (m.get(qn("w:id")) or "").lstrip("-").isdigit()),
                   default=0) + 1]
    numbering = NumberingResolver(doc)
    entries = []
    for p in body.iter(W_P):
        if _in_toc(p, toc_paragraphs):
            continue
        info = numbering.advance(p)  # every paragraph counts, as in Word
        level = levels.get(style_name(doc, p).lower())
        title = re.sub(r"\s+", " ", text_of(p)).strip()
        if level is None or not title:
            continue
        number = info.number if info is not None and not info.is_bullet else None
        entries.append(TocEntry(level, number, title, _bookmark(p, next_id, used)))
    return entries


def _run(rpr, child) -> object:
    r = w_el("r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    r.append(child)
    return r


def _text(value: str):
    t = w_el("t")
    t.text = value
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return t


def _instr(value: str):
    el = w_el("instrText")
    el.text = value
    el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return el


def _entry_ppr(prototype, level: int, right_tab: int):
    ppr = copy.deepcopy(prototype) if prototype is not None else w_el("pPr")
    for name in ("pStyle", "numPr", "outlineLvl"):
        for el in ppr.findall(qn(f"w:{name}")):
            ppr.remove(el)
    room = _FIRST_INDENT + (level - 1) * _LEVEL_STEP
    left = (level - 1) * _LEVEL_STEP + room
    tabs = w_el("tabs")
    tabs.append(w_el("tab", val="left", leader="none", pos=left))
    tabs.append(w_el("tab", val="right", leader="none", pos=right_tab))
    set_ppr_child(ppr, tabs)
    set_ppr_child(ppr, w_el("ind", left=left, right=0, hanging=room))
    return ppr


def _right_tab(ppr) -> int:
    tab = None if ppr is None else next((t for t in ppr.iter(qn("w:tab")) if t.get(qn("w:val")) == "right"), None)
    return int(tab.get(qn("w:pos"))) if tab is not None else 9072


def _entry_paragraph(entry: TocEntry, ppr, rpr, field_start: Optional[str]):
    p = w_el("p")
    p.append(ppr)
    if field_start is not None:
        p.append(_run(rpr, w_el("fldChar", fldCharType="begin")))
        p.append(_run(rpr, _instr(field_start)))
        p.append(_run(rpr, w_el("fldChar", fldCharType="separate")))
    link = w_el("hyperlink", anchor=entry.bookmark, history=1)
    if entry.number:
        link.append(_run(rpr, _text(entry.number)))
        link.append(_run(rpr, w_el("tab")))
    link.append(_run(rpr, _text(entry.title)))
    link.append(_run(rpr, w_el("tab")))
    link.append(_run(rpr, w_el("fldChar", fldCharType="begin")))
    link.append(_run(rpr, _instr(f" PAGEREF {entry.bookmark} \\h ")))
    link.append(_run(rpr, w_el("fldChar", fldCharType="separate")))
    link.append(_run(rpr, _text("" if entry.page is None else str(entry.page))))
    link.append(_run(rpr, w_el("fldChar", fldCharType="end")))
    p.append(link)
    return p


def rebuild_toc(doc, pages: Optional[dict[str, int]] = None) -> Optional[list[TocEntry]]:
    """Replace the TOC field's cached entries with the document's headings. None when there is no TOC field."""
    instr = _toc_start(doc.element.body)
    if instr is None:
        return None
    first = ancestor(instr, W_P)
    old = _field_paragraphs(first)
    field_code = "".join(i.text or "" for i in first.iter(_W_INSTR))
    entries = heading_entries(doc, toc_levels(field_code), set(old))
    for entry in entries:
        entry.page = (pages or {}).get(entry.bookmark)

    prototype_ppr = first.find(W_PPR)
    prototype_rpr = next((r.find(W_RPR) for r in first.iter(qn("w:r")) if r.find(qn("w:t")) is not None
                          and r.find(W_RPR) is not None), None)
    right_tab = _right_tab(prototype_ppr)
    new = [_entry_paragraph(e, _entry_ppr(prototype_ppr, e.level, right_tab), prototype_rpr, field_code if i == 0 else None)
           for i, e in enumerate(entries)]
    if not new:
        p = w_el("p")
        p.append(_entry_ppr(prototype_ppr, 1, right_tab))
        for child in (w_el("fldChar", fldCharType="begin"), _instr(field_code), w_el("fldChar", fldCharType="separate")):
            p.append(_run(prototype_rpr, child))
        new = [p]
    # The field's end: in its own empty paragraph when the template had one, else at the end of the last entry.
    last = old[-1]
    if last is not first and not text_of(last) and next(last.iter(qn("w:drawing")), None) is None:
        closing = copy.deepcopy(last)
        for r in [r for r in closing.findall(qn("w:r"))]:
            if r.find(_W_FLD) is None:
                closing.remove(r)
        new.append(closing)
    else:
        new[-1].append(_run(prototype_rpr, w_el("fldChar", fldCharType="end")))

    for p in new:
        first.addprevious(p)
    for p in old:
        p.getparent().remove(p)
    return entries


def toc_problems(doc) -> list[str]:
    """Post-render check: the TOC lists exactly the document's headings, and every entry's link has a target."""
    instr = _toc_start(doc.element.body)
    if instr is None:
        return []
    old = _field_paragraphs(ancestor(instr, W_P))
    field_code = "".join(i.text or "" for i in ancestor(instr, W_P).iter(_W_INSTR))
    listed = [re.sub(r"\s+", " ", "".join(t.text or "" for t in link.iter(qn("w:t")))).strip()
              for p in old for link in p.iter(qn("w:hyperlink"))]
    anchors = [link.get(qn("w:anchor")) for p in old for link in p.iter(qn("w:hyperlink"))]
    names = {m.get(qn("w:name")) for m in doc.element.body.iter(qn("w:bookmarkStart"))}
    problems = [f"table of contents links to a missing bookmark '{a}'" for a in anchors if a not in names]
    want = []
    numbering = NumberingResolver(doc)
    levels = toc_levels(field_code)
    for p in doc.element.body.iter(W_P):
        if _in_toc(p, set(old)):
            continue
        info = numbering.advance(p)
        title = re.sub(r"\s+", " ", text_of(p)).strip()
        if levels.get(style_name(doc, p).lower()) is None or not title:
            continue
        number = info.number if info is not None and not info.is_bullet else None
        want.append(f"{number}{title}" if number else title)
    # An entry's text is its number, its title and its page number, run together.
    if len(listed) != len(want) or any(not got.replace(" ", "").startswith(w.replace(" ", "")) for got, w in zip(listed, want)):
        problems.append(f"table of contents does not match the headings ({len(listed)} entries, {len(want)} headings)")
    return problems

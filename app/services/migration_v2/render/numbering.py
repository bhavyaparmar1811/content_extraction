"""Real Word list numbering from the template's own definitions (``w:numPr``, never literal "1." text).

Bullets reuse the template's first bullet list definition. Numbered lists use a
decimal definition built from that bullet definition's indents (the GP template
has no decimal list of its own); each numbered list gets its own ``w:num`` that
restarts at 1. Source sub-headings take the numbering of the template headings
(``Heading 2`` under a numbered ``Heading 1`` gives "6.1").
"""

from __future__ import annotations

import copy
import random
from typing import Optional

from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn

from .xml import W_P, set_ppr_child, w_el

_ORDERED_FORMATS = [("decimal", "%{n}."), ("lowerLetter", "%{n})"), ("lowerRoman", "%{n}.")]


def _lvl0_format(abstract) -> Optional[str]:
    lvl = abstract.find(qn("w:lvl"))
    fmt = lvl.find(qn("w:numFmt")) if lvl is not None else None
    return fmt.get(qn("w:val")) if fmt is not None else None


class ListNumbering:
    def __init__(self, doc):
        self.doc = doc
        self.root = doc.part.numbering_part.element
        abstracts = self.root.findall(qn("w:abstractNum"))
        self._bullet_abstract = next((a for a in abstracts if _lvl0_format(a) == "bullet"), None)
        self._ordered_abstract_id: Optional[str] = None
        self._bullet_num: Optional[int] = None
        self.heading_num_id = self._heading_num_id()

    # ── Lists ─────────────────────────────────────────────────────────

    def bullet_list(self) -> int:
        if self._bullet_num is None:
            self._bullet_num = self._new_num(self._bullet_abstract_id())
        return self._bullet_num

    def ordered_list(self) -> int:
        """A new numbered list, restarting at 1."""
        if self._ordered_abstract_id is None:
            self._ordered_abstract_id = self._make_ordered_abstract()
        return self._new_num(self._ordered_abstract_id, restart=True)

    @staticmethod
    def apply(ppr, num_id: int, level: int) -> None:
        """Number a paragraph: the list definition sets the indent, so the paragraph's own indent goes."""
        for ind in ppr.findall(qn("w:ind")):
            ppr.remove(ind)
        num_pr = w_el("numPr")
        num_pr.append(w_el("ilvl", val=max(0, min(level, 8))))
        num_pr.append(w_el("numId", val=num_id))
        set_ppr_child(ppr, num_pr)

    # ── Headings ──────────────────────────────────────────────────────

    def heading_style_id(self, level: int) -> Optional[str]:
        for lvl in range(min(max(level, 1), 9), 0, -1):
            try:
                style = self.doc.styles[f"Heading {lvl}"]
            except KeyError:
                continue
            if style.type == WD_STYLE_TYPE.PARAGRAPH:
                return style.style_id
        return None

    def _heading_num_id(self) -> Optional[int]:
        """The numbering the template's first-level headings carry directly, if any."""
        h1 = self.heading_style_id(1)
        for p in self.doc.element.body.iter(W_P):
            style = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
            if style is None or style.get(qn("w:val")) != h1:
                continue
            num = p.find(f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:numId')}")
            if num is not None and num.get(qn("w:val")) not in (None, "0"):
                return int(num.get(qn("w:val")))
        return None

    # ── numbering.xml ─────────────────────────────────────────────────

    def known_num_ids(self) -> set[int]:
        return {int(n.get(qn("w:numId"))) for n in self.root.findall(qn("w:num"))}

    def _bullet_abstract_id(self) -> str:
        if self._bullet_abstract is None:
            self._bullet_abstract = self._add_abstract(self._minimal_abstract("bullet"))
        return self._bullet_abstract.get(qn("w:abstractNumId"))

    def _make_ordered_abstract(self) -> str:
        if self._bullet_abstract is not None:
            abstract = copy.deepcopy(self._bullet_abstract)
            for lvl in abstract.findall(qn("w:lvl")):
                ilvl = int(lvl.get(qn("w:ilvl")))
                fmt, text = _ORDERED_FORMATS[ilvl % 3]
                for name in ("numFmt", "lvlText", "lvlPicBulletId", "rPr", "start"):
                    for node in lvl.findall(qn(f"w:{name}")):
                        lvl.remove(node)
                lvl.insert(0, w_el("start", val=1))
                lvl.insert(1, w_el("numFmt", val=fmt))
                # lvl children in schema order: start, numFmt, lvlRestart, pStyle, isLgl, suff, lvlText, ...
                before = [n for n in lvl if n.tag in {qn(f"w:{t}") for t in ("lvlRestart", "pStyle", "isLgl", "suff")}]
                (before[-1] if before else lvl[1]).addnext(w_el("lvlText", val=text.format(n=ilvl + 1)))
        else:
            abstract = self._minimal_abstract("decimal")
        return self._add_abstract(abstract).get(qn("w:abstractNumId"))

    def _add_abstract(self, abstract):
        abstracts = self.root.findall(qn("w:abstractNum"))
        new_id = max((int(a.get(qn("w:abstractNumId"))) for a in abstracts), default=-1) + 1
        abstract.set(qn("w:abstractNumId"), str(new_id))
        for name in ("nsid", "tmpl", "styleLink", "numStyleLink", "name"):
            for node in abstract.findall(qn(f"w:{name}")):
                abstract.remove(node)
        abstract.insert(0, w_el("nsid", val=f"{random.getrandbits(32):08X}"))
        if abstracts:
            abstracts[-1].addnext(abstract)
        else:
            pic_bullets = self.root.findall(qn("w:numPicBullet"))
            if pic_bullets:
                pic_bullets[-1].addnext(abstract)
            else:
                self.root.insert(0, abstract)
        return abstract

    @staticmethod
    def _minimal_abstract(kind: str):
        abstract = w_el("abstractNum", abstractNumId=0)
        abstract.append(w_el("multiLevelType", val="hybridMultilevel"))
        for ilvl in range(9):
            lvl = w_el("lvl", ilvl=ilvl)
            lvl.append(w_el("start", val=1))
            if kind == "bullet":
                lvl.append(w_el("numFmt", val="bullet"))
                lvl.append(w_el("lvlText", val=["●", "o", "▪"][ilvl % 3]))
            else:
                fmt, text = _ORDERED_FORMATS[ilvl % 3]
                lvl.append(w_el("numFmt", val=fmt))
                lvl.append(w_el("lvlText", val=text.format(n=ilvl + 1)))
            lvl.append(w_el("lvlJc", val="left"))
            ppr = w_el("pPr")
            ppr.append(w_el("ind", left=720 * (ilvl + 1), hanging=360))
            lvl.append(ppr)
            abstract.append(lvl)
        return abstract

    def _new_num(self, abstract_id: str, restart: bool = False) -> int:
        num_id = max(self.known_num_ids(), default=0) + 1
        num = w_el("num", numId=num_id)
        num.append(w_el("abstractNumId", val=abstract_id))
        if restart:
            for ilvl in range(9):
                override = w_el("lvlOverride", ilvl=ilvl)
                override.append(w_el("startOverride", val=1))
                num.append(override)
        nums = self.root.findall(qn("w:num"))
        if nums:
            nums[-1].addnext(num)
        else:
            self.root.findall(qn("w:abstractNum"))[-1].addnext(num)
        return num_id

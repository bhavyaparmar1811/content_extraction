"""Low-level OOXML helpers shared by the DOCX parsers.

- ``iter_body_blocks`` walks body paragraphs and tables in reading order,
  looking inside block-level content controls but skipping the table of
  contents.
- ``element_text`` reads all ``w:t`` text, including runs inside inline
  content controls that python-docx's ``.text`` leaves out.
- ``NumberingResolver`` reproduces Word's automatic numbering (``w:numPr``
  on the paragraph or its style chain), so a heading styled "Heading 2" in a
  numbered outline yields "6.1" even though "6.1" is not in its text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from docx.oxml.ns import qn

_W_P = qn("w:p")
_W_TBL = qn("w:tbl")
_W_SDT = qn("w:sdt")


def is_toc_sdt(sdt) -> bool:
    gallery = sdt.find(f"{qn('w:sdtPr')}/{qn('w:docPartObj')}/{qn('w:docPartGallery')}")
    return gallery is not None and "table of contents" in (gallery.get(qn("w:val")) or "").lower()


def iter_body_blocks(container) -> Iterator:
    """Yield ``w:p`` and ``w:tbl`` children in order, flattening block content controls.

    The table of contents is skipped: it repeats heading text.
    """
    for el in container:
        if el.tag == _W_SDT:
            if is_toc_sdt(el):
                continue
            content = el.find(qn("w:sdtContent"))
            if content is not None:
                yield from iter_body_blocks(content)
        elif el.tag in (_W_P, _W_TBL):
            yield el


def element_text(el, paragraph_sep: str = "\n") -> str:
    """All text under *el*, inline content controls included; paragraphs joined by *paragraph_sep*."""
    if el.tag == _W_P:
        paragraphs = [el]
    else:
        paragraphs = list(el.iter(_W_P))
    parts = []
    for p in paragraphs:
        chunks = []
        for node in p.iter(qn("w:t"), qn("w:tab"), qn("w:br")):
            if node.tag == qn("w:t"):
                chunks.append(node.text or "")
            elif node.tag == qn("w:tab"):
                chunks.append("\t")
            elif node.get(qn("w:type")) in (None, "textWrapping"):
                chunks.append("\n")
        parts.append("".join(chunks))
    return paragraph_sep.join(parts)


# ── Numbering ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NumberingInfo:
    num_id: str
    ilvl: int
    number: Optional[str]  # rendered lvlText, e.g. "6.1.2"; None for bullets
    is_bullet: bool


@dataclass
class _Level:
    start: int = 1
    fmt: str = "decimal"
    text: str = ""


def _val(el, tag: str) -> Optional[str]:
    child = el.find(qn(tag)) if el is not None else None
    return child.get(qn("w:val")) if child is not None else None


def _roman(n: int) -> str:
    out = []
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                       (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def _letter(n: int) -> str:
    # Word repeats the letter after z: a..z, aa..zz, aaa..
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def _format(n: int, fmt: str) -> str:
    if fmt == "lowerLetter":
        return _letter(n)
    if fmt == "upperLetter":
        return _letter(n).upper()
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt == "decimalZero":
        return f"{n:02d}"
    return str(n)


class NumberingResolver:
    """Computes Word list and heading numbers in document order.

    Call ``advance(p)`` once for every paragraph, in order, including empty
    ones: Word counts an empty numbered paragraph too.
    """

    def __init__(self, doc) -> None:
        self._styles = {}
        styles_el = doc.styles.element if doc.styles is not None else None
        if styles_el is not None:
            for st in styles_el.findall(qn("w:style")):
                self._styles[st.get(qn("w:styleId"))] = st

        self._abstract: dict[str, dict[int, _Level]] = {}
        self._num_abstract: dict[str, str] = {}
        self._num_overrides: dict[str, dict[int, int]] = {}
        try:
            numbering = doc.part.numbering_part.element
        except (KeyError, NotImplementedError, AttributeError):
            numbering = None
        if numbering is not None:
            for an in numbering.findall(qn("w:abstractNum")):
                levels = {}
                for lvl in an.findall(qn("w:lvl")):
                    levels[int(lvl.get(qn("w:ilvl")))] = _Level(
                        start=int(_val(lvl, "w:start") or 1),
                        fmt=_val(lvl, "w:numFmt") or "decimal",
                        text=_val(lvl, "w:lvlText") or "",
                    )
                self._abstract[an.get(qn("w:abstractNumId"))] = levels
            for num in numbering.findall(qn("w:num")):
                num_id = num.get(qn("w:numId"))
                self._num_abstract[num_id] = _val(num, "w:abstractNumId")
                overrides = {}
                for ov in num.findall(qn("w:lvlOverride")):
                    start = _val(ov, "w:startOverride")
                    if start is not None:
                        overrides[int(ov.get(qn("w:ilvl")))] = int(start)
                if overrides:
                    self._num_overrides[num_id] = overrides

        self._counters: dict[str, dict[int, int]] = {}

    # Style chain ------------------------------------------------------

    def _style_chain(self, p) -> Iterator:
        style_id = _val(p.find(qn("w:pPr")), "w:pStyle")
        seen = set()
        while style_id and style_id not in seen and style_id in self._styles:
            seen.add(style_id)
            st = self._styles[style_id]
            yield st
            style_id = _val(st, "w:basedOn")

    def effective_numpr(self, p) -> Optional[tuple[str, int]]:
        """(numId, ilvl) from the paragraph, else its style chain; None when unnumbered."""
        num_id = ilvl = None
        candidates = [p.find(qn("w:pPr"))] + [st.find(qn("w:pPr")) for st in self._style_chain(p)]
        for ppr in candidates:
            numpr = ppr.find(qn("w:numPr")) if ppr is not None else None
            if numpr is None:
                continue
            if num_id is None:
                num_id = _val(numpr, "w:numId")
            if ilvl is None:
                raw = _val(numpr, "w:ilvl")
                ilvl = int(raw) if raw is not None else None
            if num_id is not None and ilvl is not None:
                break
        if num_id is None or num_id == "0":
            return None
        return num_id, ilvl or 0

    def outline_level(self, p) -> Optional[int]:
        candidates = [p.find(qn("w:pPr"))] + [st.find(qn("w:pPr")) for st in self._style_chain(p)]
        for ppr in candidates:
            raw = _val(ppr, "w:outlineLvl")
            if raw is not None:
                level = int(raw)
                return level if level < 9 else None  # 9 = body text
        return None

    # Counting ---------------------------------------------------------

    def advance(self, p) -> Optional[NumberingInfo]:
        numpr = self.effective_numpr(p)
        if numpr is None:
            return None
        num_id, ilvl = numpr
        abstract_id = self._num_abstract.get(num_id)
        levels = self._abstract.get(abstract_id) if abstract_id is not None else None
        if not levels:
            return None

        overrides = self._num_overrides.get(num_id, {})
        key = f"num:{num_id}" if overrides else f"abs:{abstract_id}"
        counters = self._counters.setdefault(key, {})

        def start_of(level: int) -> int:
            if level in overrides:
                return overrides[level]
            return levels.get(level, _Level()).start

        counters[ilvl] = counters[ilvl] + 1 if ilvl in counters else start_of(ilvl)
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]

        level = levels.get(ilvl, _Level())
        if level.fmt in ("bullet", "none"):
            return NumberingInfo(num_id, ilvl, None, True)

        def render(match_level: int) -> str:
            value = counters.get(match_level)
            if value is None:
                value = start_of(match_level)
                counters[match_level] = value
            return _format(value, levels.get(match_level, _Level()).fmt)

        text = level.text
        for i in range(9):
            token = f"%{i + 1}"
            if token in text:
                text = text.replace(token, render(i))
        number = text.strip().rstrip(".").strip() or None
        return NumberingInfo(num_id, ilvl, number, False)

"""Template text colour: blue instruction text versus plain (kept) text.

Shared by slot detection (Phase 3), which reads blue paragraphs as instructions,
and the renderer (Phase 11), which removes them. Both must agree on what is blue.
A run's colour comes from its own ``w:color``, else its character style, else
its paragraph style (following ``basedOn``).
"""

from __future__ import annotations

from typing import Optional

from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn

from app.services.parser.template_parser import TemplateDocxParser


def style_id(el, pr: str, tag: str) -> Optional[str]:
    node = el.find(f"{qn('w:' + pr)}/{qn('w:' + tag)}")
    return node.get(qn("w:val")) if node is not None else None


def run_text(r) -> str:
    return "".join(t.text or "" for t in r.iter(qn("w:t")))


class TextColour:
    def __init__(self, doc):
        self.doc = doc
        self._cache: dict[tuple[str, str], Optional[str]] = {}

    def classify(self, el) -> Optional[str]:
        """"blue", "plain" or "mixed" by the text runs of *el*; None if it has no text."""
        blue = plain = False
        for p in ([el] if el.tag == qn("w:p") else el.iter(qn("w:p"))):
            p_style = style_id(p, "pPr", "pStyle")
            for r in p.iter(qn("w:r")):
                if not run_text(r).strip():
                    continue
                if self.run_is_blue(r, p_style):
                    blue = True
                else:
                    plain = True
        if blue and plain:
            return "mixed"
        return "blue" if blue else ("plain" if plain else None)

    def run_is_blue(self, r, p_style: Optional[str]) -> bool:
        colour = r.find(f"{qn('w:rPr')}/{qn('w:color')}")
        value = colour.get(qn("w:val")) if colour is not None else None
        if value is None:
            value = self._style_colour(style_id(r, "rPr", "rStyle"), WD_STYLE_TYPE.CHARACTER)
        if value is None:
            value = self._style_colour(p_style, WD_STYLE_TYPE.PARAGRAPH)
        return is_blue(value)

    def mark_is_blue(self, p) -> bool:
        """An empty paragraph whose paragraph mark (or an empty run) is blue: a spacer between instructions."""
        marks = [p.find(f"{qn('w:pPr')}/{qn('w:rPr')}/{qn('w:color')}")]
        marks += [r.find(f"{qn('w:rPr')}/{qn('w:color')}") for r in p.iter(qn("w:r"))]
        values = [m.get(qn("w:val")) for m in marks if m is not None]
        if not values:
            values = [self._style_colour(style_id(p, "pPr", "pStyle"), WD_STYLE_TYPE.PARAGRAPH)]
        return any(is_blue(v) for v in values)

    def _style_colour(self, style_id_: Optional[str], style_type) -> Optional[str]:
        if not style_id_:
            return None
        cache_key = (style_id_, str(style_type))
        if cache_key not in self._cache:
            colour = None
            try:
                style = self.doc.styles.get_by_id(style_id_, style_type)
                while style is not None and colour is None:
                    rgb = style.font.color.rgb if style.font.color is not None and style.font.color.type else None
                    colour = str(rgb) if rgb is not None else None
                    style = style.base_style
            except Exception:
                colour = None
            self._cache[cache_key] = colour
        return self._cache[cache_key]


def is_blue(value: Optional[str]) -> bool:
    return bool(value) and value.lower() != "auto" and TemplateDocxParser.is_blue_hex(value)

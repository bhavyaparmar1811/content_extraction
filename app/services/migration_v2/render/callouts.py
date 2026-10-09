"""Callout boxes cloned from the template's own prototypes (Phase 11).

``callout_builder.py`` (v1) is not reused: its colours and 1x1 layout don't match
the template. A box is a deep copy of the palette prototype table for its kind
(``CalloutStyle.prototype_table_index``), so fills, icon cell and widths are the
template's. Only the text cell's blocks are replaced. Without a prototype the box
is built from the ``CalloutStyle``: an icon cell and a text cell, both filled,
without borders (the icon picture itself is not available then).
"""

from __future__ import annotations

import copy
from typing import Optional

from docx.oxml.ns import qn

from app.schemas.v2 import CalloutKind, CalloutLayout, CalloutStyle

from .tables import row_cells
from .xml import (
    W_SDT,
    clear_blocks,
    ensure_ends_with_paragraph,
    set_tcpr_child,
    unwrap,
    w_el,
)


class Callouts:
    def __init__(self, palette: list[CalloutStyle], tables: list):
        """*tables* are the template's body tables, read before anything was changed."""
        self.styles = {s.kind: s for s in palette}
        self.prototypes: dict[CalloutKind, object] = {}
        for style in palette:
            index = style.prototype_table_index
            if index is not None and index < len(tables):
                proto = copy.deepcopy(tables[index])
                for sdt in list(proto.iter(W_SDT)):
                    unwrap(sdt)
                for name in ("bookmarkStart", "bookmarkEnd"):
                    for node in list(proto.iter(qn(f"w:{name}"))):
                        node.getparent().remove(node)
                self.prototypes[style.kind] = proto

    def box(self, kind: CalloutKind, blocks: list) -> Optional[object]:
        proto = self.prototypes.get(kind)
        if proto is not None:
            tbl = copy.deepcopy(proto)
            text_cell = row_cells(tbl.find(qn("w:tr")))[-1]
        else:
            style = self.styles.get(kind)
            if style is None:
                return None
            tbl, text_cell = self._built(style)
        clear_blocks(text_cell)
        for block in blocks:
            text_cell.append(block)
        ensure_ends_with_paragraph(text_cell)
        _keep_on_one_page(tbl)
        return tbl

    @staticmethod
    def _built(style: CalloutStyle):
        two = style.layout == CalloutLayout.ICON_TEXT_TWO_CELL
        widths = [round(w * 20) for w in style.column_widths_pt] or ([1390, 7636] if two else [9026])
        if two and len(widths) < 2:
            widths = [1390, max(widths[0] - 1390, 2000)]
        widths = widths[:2] if two else widths[:1]
        tbl = w_el("tbl")
        tbl_pr = w_el("tblPr")
        tbl_pr.append(w_el("tblW", w=sum(widths), type="dxa"))
        borders = w_el("tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            borders.append(w_el(side, val="nil"))
        tbl_pr.append(borders)
        tbl.append(tbl_pr)
        grid = w_el("tblGrid")
        for width in widths:
            grid.append(w_el("gridCol", w=width))
        tbl.append(grid)
        tr = w_el("tr")
        cells = []
        for width in widths:
            tc = w_el("tc")
            tcpr = w_el("tcPr")
            tc.append(tcpr)
            set_tcpr_child(tcpr, w_el("tcW", w=width, type="dxa"))
            set_tcpr_child(tcpr, w_el("shd", val="clear", color="auto", fill=style.fill_hex))
            tc.append(w_el("p"))
            tr.append(tc)
            cells.append(tc)
        tbl.append(tr)
        return tbl, cells[-1]


def _keep_on_one_page(tbl) -> None:
    """A box does not break across pages (Word still breaks a box taller than a page)."""
    for tr in tbl.findall(qn("w:tr")):
        tr_pr = tr.find(qn("w:trPr"))
        if tr_pr is None:
            tr_pr = w_el("trPr")
            tc = tr.find(qn("w:tc"))
            (tc.addprevious(tr_pr) if tc is not None else tr.append(tr_pr))
        if tr_pr.find(qn("w:cantSplit")) is None:
            tr_pr.insert(0, w_el("cantSplit"))

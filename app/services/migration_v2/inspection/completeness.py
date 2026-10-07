"""Text-loss check: is every word of the source document present in its source units?

DOCX: the body as Word stores it (``iter_body_blocks``, so the TOC is skipped,
content controls are included). PDF: the text lines of every page, minus
running headers and footers (lines in the top/bottom zone that repeat on at
least half the pages, digits ignored, so "Page 3 of 15" counts as repeating).

Words are compared against everything the units carry (text, table cells,
headers), so a word only counts as lost when it appears nowhere in the export.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.schemas.v2 import SourceDocument

_PUNCT = ".,;:()[]{}\"'“”‘’!?*•▪◦‣–—-/\\|<>"


def words(text: str) -> list[str]:
    out = []
    for raw in re.sub(r"\s+", " ", (text or "")).strip().lower().split():
        w = raw.strip(_PUNCT)
        if w and not (len(w) == 1 and not w.isalnum()):
            out.append(w)
    return out


@dataclass
class Completeness:
    source_words: int
    missing_words: list[str] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)  # source paragraphs/lines that lost words

    @property
    def recall(self) -> float:
        if not self.source_words:
            return 1.0
        return round(1 - len(self.missing_words) / self.source_words, 4)


def _unit_words(units: SourceDocument) -> set[str]:
    return set(words(json.dumps(units.to_clean_dict(), ensure_ascii=False)))


def _check(blocks: list[str], units: SourceDocument) -> Completeness:
    present = _unit_words(units)
    total, missing, context = 0, [], []
    for block in blocks:
        block_words = words(block)
        total += len(block_words)
        lost = [w for w in block_words if w not in present]
        if lost:
            missing += lost
            context.append(re.sub(r"\s+", " ", block).strip()[:300])
    return Completeness(source_words=total, missing_words=missing, missing_context=context)


def docx_blocks(path: Path) -> list[str]:
    from docx import Document

    from app.services.parser.ooxml import element_text, iter_body_blocks

    body = Document(str(path)).element.body
    return [element_text(block, " ") for block in iter_body_blocks(body)]


def pdf_blocks(path: Path) -> list[str]:
    """Body lines of a PDF, without running headers and footers."""
    import fitz

    pages = []
    with fitz.open(str(path)) as pdf:
        for page in pdf:
            height = page.rect.height or 842.0
            lines = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(span["text"] for span in line["spans"]).strip()
                    if text:
                        y0, y1 = line["bbox"][1], line["bbox"][3]
                        lines.append((text, y1 <= height * 0.14 or y0 >= height * 0.88))
            pages.append(lines)

    def key(text: str) -> str:
        text = re.sub(r"\d+", "#", re.sub(r"\s+", " ", text).strip().lower())
        return text.strip(" .,;:")

    threshold = max(2, len(pages) // 2)
    edge_counts: Counter = Counter()
    any_counts: Counter = Counter()
    for lines in pages:
        edge_counts.update({key(t) for t, edge in lines if edge})
        any_counts.update({key(t) for t, _ in lines})
    running = {k for k, n in edge_counts.items() if n >= threshold}
    # Watermarks ("Working Copy") repeat mid-page: short lines found on most pages.
    running |= {k for k, n in any_counts.items() if n >= threshold and len(k.split()) <= 4 and len(pages) >= 3}
    return [t for lines in pages for t, edge in lines if key(t) not in running]


def check_completeness(path: Path, units: SourceDocument) -> Completeness:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        return _check(pdf_blocks(path), units)
    return _check(docx_blocks(path), units)

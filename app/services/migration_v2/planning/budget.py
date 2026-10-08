"""Token budgeting for planner LLM calls: estimate prompt size and pack sections into coherent blocks.

A block is one LLM call. Whole target sections are packed together while they
fit the budget. A section larger than the budget is split, but only at a
coherent boundary: the start of a source sub-section, or before a passage that
is not a list item (so a list stays with the line that introduces it).
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHARS_PER_TOKEN = 4  # rough for English prose with gpt-4o's tokenizer; errs on the high side for IDs


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


@dataclass
class Line:
    """One prompt line about one passage or table."""

    text: str
    unit_ids: list[str]
    break_ok: bool = True  # a block may start at this line


@dataclass
class SectionLines:
    target_section_id: str
    header: str            # repeated at the top of every block that holds part of the section
    lines: list[Line]


@dataclass
class Block:
    parts: list[SectionLines] = field(default_factory=list)
    tokens: int = 0

    @property
    def target_section_ids(self) -> list[str]:
        return list(dict.fromkeys(p.target_section_id for p in self.parts))

    @property
    def unit_ids(self) -> set[str]:
        return {u for p in self.parts for line in p.lines for u in line.unit_ids}

    def render(self) -> str:
        out = []
        for part in self.parts:
            out.append(part.header)
            out.extend(line.text for line in part.lines)
        return "\n".join(out)


def _size(lines: list[Line]) -> int:
    return sum(estimate_tokens(line.text) for line in lines)


def split_section(section: SectionLines, budget: int) -> list[SectionLines]:
    """Chunks of a section, each within the budget where a boundary allows it."""
    header_cost = estimate_tokens(section.header)
    if header_cost + _size(section.lines) <= budget:
        return [section]
    chunks: list[list[Line]] = [[]]
    size = header_cost
    for line in section.lines:
        cost = estimate_tokens(line.text)
        if chunks[-1] and size + cost > budget and line.break_ok:
            chunks.append([])
            size = header_cost
        chunks[-1].append(line)
        size += cost
    total = len(chunks)
    return [
        SectionLines(section.target_section_id, f"{section.header} (part {i} of {total})" if total > 1 else section.header, c)
        for i, c in enumerate(chunks, start=1)
    ]


def pack(sections: list[SectionLines], budget: int) -> list[Block]:
    """Pack sections, in order, into blocks of at most ``budget`` tokens (a single oversized chunk stays alone)."""
    blocks: list[Block] = []
    current = Block()
    for section in sections:
        for chunk in split_section(section, budget):
            cost = estimate_tokens(chunk.header) + _size(chunk.lines)
            if current.parts and current.tokens + cost > budget:
                blocks.append(current)
                current = Block()
            current.parts.append(chunk)
            current.tokens += cost
    if current.parts:
        blocks.append(current)
    return blocks

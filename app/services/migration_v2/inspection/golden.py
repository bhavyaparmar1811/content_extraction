"""Comparisons with the reviewed golden expectations (``documents/golden/<sop stem>.json``)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from app.schemas.v2 import ClaimKind, MappingType, SectionDraft, SectionPlan, SlotPlan, SourceDocument, SourceSection, TemplateModel


def _norm(text: str) -> str:
    text = re.sub(r"\(optional\)", "", text or "", flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", text.lower())).strip()


def find_golden(golden_dir: Path, source_file: str) -> Optional[dict]:
    """The golden whose ``sop_file`` is this SOP, if any."""
    golden_dir = Path(golden_dir)
    if not golden_dir.exists():
        return None
    name = Path(source_file).name
    for path in sorted(golden_dir.glob("*.json")):
        try:
            golden = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if golden.get("sop_file") == name:
            return golden
    return None


def golden_problems(plan: SectionPlan, source: SourceDocument, tpl: TemplateModel, golden: dict) -> list[str]:
    """Where the plan disagrees with a golden ``section_mapping``. Splits may be a split or a move of the subsection."""
    targets = {_norm(t.heading): t.section_id for t in tpl.sections}
    by_path: dict[str, SourceSection] = {}
    by_id = {s.section_id: s for s in source.sections}
    for s in source.sections:
        path, node = [], s
        while node is not None:
            path.append(_norm(node.heading))
            node = by_id.get(node.parent_id) if node.parent_id else None
        by_path[" > ".join(reversed(path))] = s

    unit_target: dict[str, str] = {}
    for m in plan.mappings:
        if not m.target_section_id or m.mapping_type in (MappingType.OMIT, MappingType.UNRESOLVED):
            continue
        listed = set(m.unit_ids)
        for sid in m.source_section_ids:
            units = [u.unit_id for u in by_id[sid].units]
            covered = [u for u in units if u in listed] if (m.mapping_type == MappingType.SPLIT and listed & set(units)) else units
            for u in covered:
                unit_target[u] = m.target_section_id

    rows = golden.get("section_mapping", [])
    explicit = {" > ".join(_norm(p) for p in r["source_heading"].split(">")) for r in rows if r.get("source_heading")}
    problems = []
    for row in rows:
        target_id = targets.get(_norm(row["target_heading"]))
        if not row.get("source_heading"):
            m = next((x for x in plan.mappings if x.target_section_id == target_id), None)
            if m is None or m.source_section_ids:
                problems.append(f"{row['target_heading']}: expected no source content")
            continue
        key = " > ".join(_norm(p) for p in row["source_heading"].split(">"))
        section = by_path.get(key)
        if section is None:
            problems.append(f"source heading {row['source_heading']!r} not found")
            continue
        stack, units = [section], []
        while stack:
            node = stack.pop()
            units += [u.unit_id for u in node.units if not u.is_boilerplate]
            for child in source.sections:
                if child.parent_id == node.section_id:
                    child_key = next(k for k, v in by_path.items() if v is child)
                    if child_key not in explicit:
                        stack.append(child)
        wrong = [u for u in units if unit_target.get(u) != target_id]
        if wrong:
            got = sorted({unit_target.get(u, "unplaced") for u in wrong})
            problems.append(f"{row['source_heading']!r} → {row['target_heading']}: {len(wrong)} unit(s) went to {got}")
    return problems


def _words(text: str) -> str:
    """Letters and digits only: robust to quotes, dashes and the odd mis-decoded character in goldens."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _units_containing(source: SourceDocument, needle: str) -> list[str]:
    want = _words(needle)
    return [u.unit_id for u in source.iter_units() if not u.is_boilerplate and want and want in _words(u.text)]


def _slot_places(plan: SlotPlan) -> dict[str, set[tuple[str, str]]]:
    """unit_id → {(target_section_id, slot_id)} as the slot plan places it."""
    out: dict[str, set[tuple[str, str]]] = {}
    for section in plan.sections:
        for m in section.slot_mappings:
            for u in m.source_unit_ids:
                out.setdefault(u, set()).add((section.target_section_id, m.slot_id))
    return out


def slot_problems(plan: SlotPlan, source: SourceDocument, tpl: TemplateModel, golden: dict) -> list[str]:
    """Where the slot plan disagrees with the golden ``slot_expectations`` and ``expected_empty_slots``.

    ``target_slot_ref`` names the exact slot. ``target_slot`` is a content type: the passage must land in a slot
    of that type in the target section, or in the section's content slot when the template has no such slot.
    """
    sections = {_norm(t.heading): t for t in tpl.sections}
    places = _slot_places(plan)
    problems = []
    for exp in golden.get("slot_expectations", []):
        needle = exp["source_text_contains"]
        units = _units_containing(source, needle)
        if not units:
            problems.append(f"slot expectation: text not found in any passage: {needle[:60]!r}")
            continue
        got = set().union(*(places.get(u, set()) for u in units))
        if exp.get("target_slot_ref"):
            slot = tpl.slot_by_ref(exp["target_slot_ref"])
            if slot is None:
                problems.append(f"slot expectation: unknown slot {exp['target_slot_ref']}")
            elif not any(s == slot.slot_id for _, s in got):
                problems.append(f"{needle[:50]!r} → expected {exp['target_slot_ref']}, got "
                                f"{sorted(_ref(tpl, s) for _, s in got) or 'no slot'}")
            continue
        target = sections.get(_norm(exp.get("target_heading", "")))
        if target is None:
            problems.append(f"slot expectation: unknown target section {exp.get('target_heading')!r}")
            continue
        content = [s for s in target.slots if s.callout_kind is None]
        typed = [s.slot_id for s in content if s.content_type.value == exp.get("target_slot")]
        allowed = set(typed) or {s.slot_id for s in content}
        if not any(sec == target.section_id and s in allowed for sec, s in got):
            problems.append(f"{needle[:50]!r} → expected a {exp.get('target_slot')} slot of {target.key}, got "
                            f"{sorted(_ref(tpl, s) for _, s in got) or 'no slot'}")
    for exp in golden.get("expected_empty_slots", []):
        slot = tpl.slot_by_ref(exp["slot_ref"])
        mapping = next((m for s in plan.sections for m in s.slot_mappings if slot and m.slot_id == slot.slot_id), None)
        if mapping is not None and mapping.source_unit_ids:
            problems.append(f"{exp['slot_ref']} should stay empty ({exp.get('reason', '')}) but holds {mapping.source_unit_ids}")
    return problems


def _ref(tpl: TemplateModel, slot_id: str) -> str:
    for section in tpl.sections:
        for slot in section.slots:
            if slot.slot_id == slot_id:
                return f"{section.key or section.section_id}.{slot.key or slot.slot_id}"
    return slot_id


def callout_comparison(plan: SlotPlan, source: SourceDocument, golden: dict) -> list[tuple[str, str, str]]:
    """(golden text, suggested kind, what the plan did) for each ``callout_candidates`` entry; informational only."""
    kinds = {u: a.kind.value for s in plan.sections for a in s.callout_assignments for u in a.unit_ids}
    out = []
    for cand in golden.get("callout_candidates", []):
        units = _units_containing(source, cand["source_text_contains"])
        got = sorted({kinds[u] for u in units if u in kinds})
        out.append((cand["source_text_contains"], cand["kind"], ", ".join(got) if got else "not promoted"))
    return out


def draft_heading_problems(drafts: list[SectionDraft], golden: dict) -> list[str]:
    """The golden ``subsections_preserved`` must appear as heading claims, in this order."""
    headings = [_words(c.text) for d in drafts for c in d.iter_claims() if c.kind == ClaimKind.HEADING]
    problems, at = [], 0
    for wanted in golden.get("subsections_preserved", []):
        key = _words(wanted)
        hit = next((i for i in range(at, len(headings)) if key in headings[i]), None)
        if hit is None:
            where = "out of order" if any(key in h for h in headings) else "missing"
            problems.append(f"sub-heading {wanted!r} is {where} in the draft")
        else:
            at = hit + 1
    return problems


def draft_missing_must_preserve(drafts: list[SectionDraft], source: SourceDocument, golden: dict) -> list[str]:
    """Golden must-preserve strings the source has but the drafted text does not (reference tokens read back)."""
    from ..drafting.refs import detokenized

    text = _words(" ".join(c.text for d in detokenized(drafts, source) for c in d.iter_claims()))
    in_source = set(golden.get("must_preserve", [])) - set(missing_must_preserve(source, golden))
    return [fact for fact in golden.get("must_preserve", []) if fact in in_source and _words(fact) not in text]


def missing_must_preserve(source: SourceDocument, golden: dict) -> list[str]:
    """Golden must-preserve strings that do not appear in the units' text."""
    text = re.sub(r"\s+", " ", " ".join(u.text for u in source.iter_units())).lower()
    return [fact for fact in golden.get("must_preserve", []) if re.sub(r"\s+", " ", fact).strip().lower() not in text]


def draft_figure_problems(drafts: list[SectionDraft], golden: dict) -> list[str]:
    """Each golden figure must be drafted: its caption as a caption claim, with a figure claim in the same slot."""
    problems = []
    for figure in golden.get("figures", []):
        key = _words(figure.get("caption_contains", ""))
        slots = [s for d in drafts for s in d.slots
                 if any(c.kind == ClaimKind.CAPTION and key in _words(c.text) for c in s.claims)]
        if not slots:
            problems.append(f"figure {figure.get('caption_contains')!r} is not in the draft (no caption claim)")
        elif not any(c.kind == ClaimKind.FIGURE for s in slots for c in s.claims):
            problems.append(f"figure {figure.get('caption_contains')!r}: the caption is drafted but not the image")
    return problems


def xref_problems(assembled, number_map, drafts: list[SectionDraft], tpl: TemplateModel, golden: dict) -> list[str]:
    """Each golden cross-reference must land on its expected heading (and list entry, for "#n")."""
    if assembled is None or number_map is None:
        return []
    claims = {c.claim_id: c for d in drafts for c in d.iter_claims()}
    template_headings = {s.section_id: s.heading for s in tpl.sections}
    heading_of: dict[str, str] = {}  # bookmark → the heading text it marks
    for e in number_map.entries:
        if e.kind.value == "section" and e.source_id == e.target_section_id:
            heading_of[e.bookmark] = template_headings.get(e.target_section_id, "")
        elif e.kind.value == "heading" and e.claim_id in claims:
            heading_of[e.bookmark] = claims[e.claim_id].text
    problems = []
    for expected in golden.get("cross_references", []):
        phrase = _words(expected.get("source_text", ""))
        ref = next((r for r in assembled.refs if _words(r.source_phrase) and _words(r.source_phrase) in phrase), None)
        entry = number_map.lookup(ref.target) if ref else None
        if ref is None or entry is None:
            problems.append(f"reference {expected.get('source_text')!r} does not point to anything in the new document")
            continue
        heading = heading_of.get(entry.bookmark, "")
        want = re.split(r"[#(]", expected.get("expected_target_heading", ""))[0].split(">")[-1]
        if _words(want) and _words(want) not in _words(heading) and _words(heading) not in _words(want):
            problems.append(f"reference {ref.source_phrase!r} points to {heading!r} ({entry.target_number}), "
                            f"expected {expected.get('expected_target_heading')!r}")
        item = re.search(r"#(\d+)", expected.get("expected_target_heading", ""))
        if item and entry.item_number != item.group(1):
            problems.append(f"reference {ref.source_phrase!r} points to entry {entry.item_number}, expected #{item.group(1)}")
    return problems

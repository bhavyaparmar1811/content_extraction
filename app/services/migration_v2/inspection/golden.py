"""Comparisons with the reviewed golden expectations (``documents/golden/<sop stem>.json``)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from app.schemas.v2 import MappingType, SectionPlan, SourceDocument, SourceSection, TemplateModel


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


def missing_must_preserve(source: SourceDocument, golden: dict) -> list[str]:
    """Golden must-preserve strings that do not appear in the units' text."""
    text = re.sub(r"\s+", " ", " ".join(u.text for u in source.iter_units())).lower()
    return [fact for fact in golden.get("must_preserve", []) if re.sub(r"\s+", " ", fact).strip().lower() not in text]

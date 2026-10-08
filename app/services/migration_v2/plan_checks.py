"""Reference checks for section and slot plans: every ID a plan names must exist.

Run on every human edit. The planners' full validators (Phases 7 and 8:
coverage, order, required sections) build on these.
"""

from __future__ import annotations

from app.schemas.v2 import SectionPlan, SlotPlan, SourceDocument, TemplateModel


def section_plan_problems(plan: SectionPlan, source: SourceDocument, template: TemplateModel) -> list[str]:
    targets = {s.section_id for s in template.sections}
    sources = {s.section_id for s in source.sections}
    units = {u.unit_id for u in source.iter_units()}
    problems = []
    for i, m in enumerate(plan.mappings):
        where = f"mappings[{i}]"
        if m.target_section_id and m.target_section_id not in targets:
            problems.append(f"{where}: unknown target section {m.target_section_id!r}")
        problems += [f"{where}: unknown source section {s!r}" for s in m.source_section_ids if s not in sources]
        problems += [f"{where}: unknown unit {u!r}" for u in m.unit_ids if u not in units]
    return problems


def slot_plan_problems(plan: SlotPlan, source: SourceDocument, template: TemplateModel) -> list[str]:
    slots_by_section = {s.section_id: {slot.slot_id for slot in s.slots} for s in template.sections}
    sources = {s.section_id for s in source.sections}
    units = {u.unit_id for u in source.iter_units()}
    palette = {c.kind for c in template.callout_palette}
    problems = []
    for section in plan.sections:
        where = section.target_section_id
        if where not in slots_by_section:
            problems.append(f"unknown target section {where!r}")
            continue
        problems += [f"{where}: unknown source section {s!r}" for s in section.source_section_ids if s not in sources]
        for m in section.slot_mappings:
            if m.slot_id not in slots_by_section[where]:
                problems.append(f"{where}: slot {m.slot_id!r} is not in this section")
            problems += [f"{where}/{m.slot_id}: unknown unit {u!r}" for u in m.source_unit_ids if u not in units]
        problems += [f"{where}: unknown unplaced unit {u!r}" for u in section.unplaced_unit_ids if u not in units]
        for a in section.callout_assignments:
            if a.kind not in palette:
                problems.append(f"{where}: callout kind {a.kind.value!r} is not in the template palette")
            problems += [f"{where}: unknown callout unit {u!r}" for u in a.unit_ids if u not in units]
    return problems

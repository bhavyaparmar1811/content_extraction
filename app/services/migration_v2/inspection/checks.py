"""Pass / warn / fail checks shown at the top of an inspection report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.schemas.v2 import MappingStatus, ReadinessStatus

if TYPE_CHECKING:
    from .runner import Inspection

PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


def _completeness(insp: "Inspection") -> Check:
    c = insp.completeness
    pct = f"{c.recall * 100:.2f}%"
    if not c.missing_words:
        return Check("Text completeness", PASS, f"all {c.source_words} source words are in the passages ({pct})")
    status = WARN if c.recall >= 0.99 else FAIL
    return Check("Text completeness", status, f"{len(c.missing_words)} of {c.source_words} words missing ({pct} kept)",
                 [f"lost from: {ctx}" for ctx in c.missing_context[:30]])


def _structure(insp: "Inspection") -> Check:
    doc = insp.source
    parents = {s.parent_id for s in doc.sections if s.parent_id}
    empty = [s for s in doc.sections if not s.units and s.section_id not in parents]
    units = list(doc.iter_units())
    boilerplate = sum(u.is_boilerplate for u in units)
    top = [s for s in doc.sections if s.parent_id is None and s.number != "0"]
    summary = (f"{len(doc.sections)} sections ({len(top)} chapters), {len(units)} passages, "
               f"{boilerplate} marked boilerplate (cover page, history)")
    details = [f"empty section: {s.section_id} {s.heading!r}" for s in empty]
    if not any(s.number == "0" for s in doc.sections):
        details.append("no cover-page section (number 0) was detected")
    if len(top) < 3:
        return Check("Structure", FAIL, summary + "; fewer than 3 chapters were detected", details)
    return Check("Structure", WARN if details else PASS, summary, details)


def _template(insp: "Inspection") -> Check:
    t = insp.template
    report = t.readiness
    slots = sum(len(s.slots) for s in t.model.sections)
    summary = f"{len(t.model.sections)} sections, {slots} slots, readiness '{report.status.value}'"
    details = [f"{i.code}: {i.message} ({i.ref})" for i in report.blocking]
    details += [f"region {r.region_id} ({r.kind}, {r.section_key}): decide '{r.suggestion}'? — {r.text[:120]}"
                for r in report.ambiguous_regions if not r.decision]
    if t.error:
        details.insert(0, t.error)
    if report.status == ReadinessStatus.READY:
        return Check("Template", PASS, summary, details)
    return Check("Template", FAIL, summary + "; a real migration job would refuse this template", details)


def _facts(insp: "Inspection") -> Check:
    f = insp.facts
    kinds: dict[str, int] = {}
    for v in f.values:
        kinds[v.kind.value] = kinds.get(v.kind.value, 0) + 1
    mods: dict[str, int] = {}
    for o in f.obligations:
        mods[o.modality.value] = mods.get(o.modality.value, 0) + 1
    summary = (f"{len(f.values)} values ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items())) or 'none'}), "
               f"{len(f.obligations)} obligations ({', '.join(f'{k} {n}' for k, n in sorted(mods.items())) or 'none'}), "
               f"{len(f.roles)} roles, {len(f.approved_terms)} terms")
    if insp.self_check:
        return Check("Protected facts", FAIL, summary + "; the verbatim self-check raised issues",
                     [i.message for i in insp.self_check[:30]])
    return Check("Protected facts", PASS, summary + "; verbatim self-check clean")


def _plan(insp: "Inspection") -> Check:
    report, plan = insp.plan_report, insp.plan
    gates = report.open_gate_issues
    review = [m for m in plan.mappings if m.status == MappingStatus.NEEDS_REVIEW and m.target_section_id]
    summary = (f"{len([m for m in plan.mappings if m.source_section_ids])} mappings, unit coverage "
               f"{report.unit_coverage * 100:.0f}%, {len(gates)} blocking issue(s), {len(review)} to review")
    details = [f"BLOCKING: {i.message}" for i in gates] + [i.message for i in report.issues if i.gate is None]
    if gates:
        return Check("Section plan", FAIL, summary, details)
    return Check("Section plan", WARN if review else PASS, summary, details)


def _golden(insp: "Inspection") -> Check:
    if insp.golden is None:
        return Check("Golden comparison", INFO, "no golden expectation for this SOP")
    details = insp.golden_problems + [f"must-preserve text not found: {m!r}" for m in insp.missing_preserve]
    if details:
        return Check("Golden comparison", FAIL, f"{len(details)} difference(s) from the reviewed golden", details)
    return Check("Golden comparison", PASS, "section mapping and must-preserve facts match the golden")


def _llm(insp: "Inspection") -> Check:
    usage = insp.plan.token_usage
    if not usage:
        llm_events = [e for e in insp.events if e["event"] == "section_planner_llm_failed"]
        if llm_events:
            return Check("LLM check", WARN, "the LLM call failed; the rule plan was kept",
                         [str(e["detail"]) for e in llm_events])
        return Check("LLM check", INFO, "not run (rules only); use --llm to run the confirm call")
    return Check("LLM check", PASS,
                 f"{usage.get('calls', 0)} call(s), {usage.get('input_tokens', 0)} input / "
                 f"{usage.get('output_tokens', 0)} output tokens ({insp.plan.model}, {insp.plan.prompt_version})")


def evaluate(insp: "Inspection") -> list[Check]:
    return [_completeness(insp), _structure(insp), _template(insp), _facts(insp), _plan(insp), _golden(insp), _llm(insp)]


def overall(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    return FAIL if FAIL in statuses else WARN if WARN in statuses else PASS


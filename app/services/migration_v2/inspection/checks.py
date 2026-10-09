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


def _slots(insp: "Inspection") -> Check:
    report, plan = insp.slot_report, insp.slot_plan
    gates = report.open_gate_issues
    mappings = [m for s in plan.sections for m in s.slot_mappings]
    filled = sum(bool(m.source_unit_ids) for m in mappings)
    gaps = sum(m.status == MappingStatus.SOURCE_CONTENT_NOT_FOUND for m in mappings)
    callouts = sum(len(s.callout_assignments) for s in plan.sections)
    review = [i for i in report.issues if i.gate is None]
    summary = (f"{filled} of {len(mappings)} slots filled, {gaps} gap(s), {callouts} callout(s), unit coverage "
               f"{report.unit_coverage * 100:.0f}%, {len(gates)} blocking issue(s), {len(review)} to review")
    details = [f"BLOCKING: {i.message}" for i in gates] + [i.message for i in review]
    if gates:
        return Check("Slot plan", FAIL, summary, details)
    return Check("Slot plan", WARN if review else PASS, summary, details)


def _draft(insp: "Inspection") -> Check:
    report = insp.draft_report
    claims = [c for d in insp.drafts for c in d.iter_claims()]
    gates = report.open_gate_issues
    review = [i for i in report.issues if i.gate is None]
    content = [c for c in claims if not c.is_gap_marker and c.kind.value != "heading"]
    rewritten = sum(bool(c.rule_ids_applied) for c in content)
    mode = "GWP rewrite" if insp.rules.guide_id != "BASELINE" else "placement mode (copied as written)"
    summary = (f"{mode}: {len(content)} claims ({rewritten} with GWP rules applied), "
               f"{sum(c.kind.value == 'heading' for c in claims)} sub-headings, {sum(c.is_gap_marker for c in claims)} gap marker(s), "
               f"coverage {report.unit_coverage * 100:.0f}%, {len(gates)} blocking issue(s), {len(review)} to review")
    details = [f"BLOCKING: {i.message}" for i in gates] + [i.message for i in review]
    if gates:
        return Check("Draft", FAIL, summary, details)
    return Check("Draft", WARN if review else PASS, summary, details)


def _quality(insp: "Inspection") -> Check:
    """Gates over the last validation round. Gaps and high-risk findings wait for a reviewer (WARN); any other open
    gate is a defect the loop could not repair (FAIL)."""
    from ..quality.gates import awaits_reviewer

    report = insp.quality
    open_ = [i for i in report.issues if not i.resolved]
    gaps = [i for i in report.open_gate_issues if awaits_reviewer(i)]
    blocking = [i for i in report.open_gate_issues if not awaits_reviewer(i)]
    rounds = [e["detail"] for e in insp.events if e["event"] == "validation"]
    repairs = [e["detail"] for e in insp.events if e["event"] == "repair"]
    critic = sum(r["critic"]["findings"] for r in rounds)
    scores = ", ".join(f"{k} {v:g}" for k, v in sorted(report.soft_scores.items()))
    summary = (f"job would end {insp.final_status}: {len(blocking)} blocking issue(s), {len(gaps)} gap(s) or high-risk "
               "finding(s) for the reviewer, "
               f"{len(rounds)} validation round(s), {len(repairs)} repair round(s), {critic} critic finding(s), "
               f"{len(report.high_risk_units)} high-risk passage(s)" + (f"; {scores}" if scores else ""))
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    details = ([f"BLOCKING: {i.message}" for i in blocking] + [f"REVIEWER: {i.message}" for i in gaps]
               + [f"{i.severity.value}: {i.message}" for i in sorted(open_, key=lambda i: order[i.severity.value])
                  if i.gate is None and i.severity.value in ("critical", "high", "medium")])
    if blocking:
        return Check("Quality gates", FAIL, summary, details)
    return Check("Quality gates", WARN if gaps or any(i.severity.value != "low" for i in open_) else PASS, summary, details)


def _document(insp: "Inspection") -> Check:
    """The Word review draft: every slot filled, marked as a gap or removed as planned, and the post-render check."""
    report = insp.render
    if report is None:
        failed = [e["detail"].get("error", "") for e in insp.events if e["event"] == "render_failed"]
        return Check("Word document", FAIL if failed else INFO, "not rendered" + (f": {failed[0]}" if failed else ""))
    counts: dict[str, int] = {}
    for slot in report.slots:
        counts[slot.outcome.value] = counts.get(slot.outcome.value, 0) + 1
    regions = ", ".join(f"{r.region_id} {r.outcome.value}{' ' + repr(r.choice) if r.choice else ''}" for r in report.regions)
    summary = (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()))
               + (f"; sections removed: {', '.join(report.sections_removed)}" if report.sections_removed else "")
               + (f"; regions: {regions}" if regions else ""))
    details = ([f"PROBLEM: {p}" for p in report.problems] + [f"no anchor: {s.slot_id}" for s in report.slots
                                                              if s.outcome.value == "no_anchor"]
               + [f"region {r.region_id} removed: {r.note}" for r in report.regions if r.outcome.value == "removed"]
               + report.warnings)
    if report.problems or counts.get("no_anchor"):
        return Check("Word document", FAIL, summary, details)
    return Check("Word document", WARN if details else PASS, summary, details)


def _golden(insp: "Inspection") -> Check:
    if insp.golden is None:
        return Check("Golden comparison", INFO, "no golden expectation for this SOP")
    details = (insp.golden_problems + insp.golden_slot_problems + insp.golden_draft_problems
               + [f"must-preserve text not found: {m!r}" for m in insp.missing_preserve])
    if details:
        return Check("Golden comparison", FAIL, f"{len(details)} difference(s) from the reviewed golden", details)
    extra = [f"reworded by the GWP rewrite (check the meaning): {m!r}" for m in insp.draft_paraphrased]
    return Check("Golden comparison", WARN if extra else PASS,
                 "section mapping, slot placements, empty slots, sub-headings and must-preserve facts match", extra)


def _summed(usages) -> dict:
    total: dict = {}
    for usage in usages:
        for k, v in usage.items():
            total[k] = total.get(k, 0) + v
    return total if total.get("calls") else {}


def _references(insp: "Inspection") -> Check:
    refs = insp.assembled.refs if insp.assembled else []
    if insp.assembled is None:
        return Check("Cross-references", INFO, "not assembled")
    if not refs:
        return Check("Cross-references", INFO, "no internal cross-reference in the SOP")
    counts = {s: sum(r.status.value == s for r in refs) for s in ("resolved", "merged", "unresolved")}
    changed = [r for r in refs if r.text != r.source_phrase]
    summary = (f"{len(refs)} reference(s): {counts['resolved']} resolved, {counts['merged']} merged, "
               f"{counts['unresolved']} unresolved; {len(changed)} with a new number")
    details = [f"{r.claim_id}: '{r.source_phrase}' → '{r.text}' ({r.status.value}){' — ' + r.note if r.note else ''}"
               for r in refs if r.status.value != "resolved" or r.text != r.source_phrase]
    status = FAIL if counts["unresolved"] else WARN if counts["merged"] else PASS
    return Check("Cross-references", status, summary, details)


def _llm(insp: "Inspection") -> Check:
    calls = [e["detail"] for e in insp.events if e["event"] == "llm_call"]
    if calls:  # Phase 12: one audited event per call
        tasks: dict[str, dict] = {}
        for c in calls:
            t = tasks.setdefault(c.get("task") or "unknown", {"calls": 0, "in": 0, "out": 0, "ms": 0, "retries": 0, "errors": 0,
                                                              "version": c.get("prompt_version")})
            t["calls"] += 1
            t["in"] += c.get("input_tokens", 0)
            t["out"] += c.get("output_tokens", 0)
            t["ms"] += c.get("latency_ms", 0)
            t["retries"] += bool(c.get("retry"))
            t["errors"] += c.get("status") != "ok"
        parts = [f"{name} ({t['version']}): {t['calls']} call(s), {t['in']} in / {t['out']} out, {t['ms'] / 1000:.0f}s"
                 + (f", {t['retries']} retr{'y' if t['retries'] == 1 else 'ies'}" if t["retries"] else "")
                 + (f", {t['errors']} failed" if t["errors"] else "") for name, t in tasks.items()]
        model = ", ".join(sorted({str(c.get("model")) for c in calls}))
        failed = [f"{c.get('task')}: {c.get('error') or c.get('status')}" for c in calls if c.get("status") != "ok"]
        return Check("LLM check", WARN if failed else PASS, "; ".join(parts) + f" ({model})", failed)
    return _llm_from_stage_events(insp)


def _llm_from_stage_events(insp: "Inspection") -> Check:
    drafter = next((e["detail"] for e in insp.events if e["event"] == "drafter"), {})
    stages = [("section plan", insp.plan.token_usage, insp.plan.model, insp.plan.prompt_version),
              ("slot plan", insp.slot_plan.token_usage, insp.slot_plan.model, insp.slot_plan.prompt_version),
              ("draft", drafter.get("usage") if drafter.get("llm") else {},
               next((d.model for d in insp.drafts if d.model), None), next((d.prompt_version for d in insp.drafts if d.prompt_version), None)),
              ("critic", _summed(e["detail"]["critic"]["usage"] for e in insp.events if e["event"] == "validation"),
               insp.slot_plan.model, next((e["detail"]["critic"]["prompt_version"] for e in insp.events
                                           if e["event"] == "validation" and e["detail"]["critic"]["prompt_version"]), None)),
              ("repair", _summed(e["detail"]["usage"] for e in insp.events if e["event"] == "repair" and e["detail"]["llm"]),
               insp.slot_plan.model, None)]
    failed = [e for e in insp.events if e["event"] in ("section_planner_llm_failed", "slot_planner_llm_failed")]
    ran = [(name, u, m, p) for name, u, m, p in stages if u]
    if not ran:
        if failed:
            return Check("LLM check", WARN, "the LLM call failed; the rule plan was kept", [str(e["detail"]) for e in failed])
        return Check("LLM check", INFO, "not run (rules only); use --llm to run the confirm calls")
    parts = [f"{name}: {u.get('calls', 0)} call(s), {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out"
             for name, u, _, _ in ran]
    model = ", ".join(sorted({f"{m}, {p}" for _, _, m, p in ran}))
    return Check("LLM check", WARN if failed else PASS, "; ".join(parts) + f" ({model})",
                 [str(e["detail"]) for e in failed])


def evaluate(insp: "Inspection") -> list[Check]:
    return [_completeness(insp), _structure(insp), _template(insp), _facts(insp), _plan(insp), _slots(insp),
            _draft(insp), _quality(insp), _references(insp), _document(insp), _golden(insp), _llm(insp)]


def overall(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    return FAIL if FAIL in statuses else WARN if WARN in statuses else PASS


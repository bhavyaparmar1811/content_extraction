"""Self-contained HTML reports for inspections: one page per SOP, plus an index.

No external resources (the SOPs are confidential and the files stay local).
All document text is HTML-escaped.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable

from app.schemas.v2 import MappingStatus, MappingType, SectionPlan, SourceDocument, SourceUnit

from .checks import FAIL, INFO, PASS, WARN, Check, overall
from .runner import Inspection

_CSS = """
:root { --pass:#1a7f37; --warn:#9a6700; --fail:#cf222e; --info:#57606a; --line:#d0d7de; --soft:#f6f8fa; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; margin: 0; color: #1f2328; background: #fff; }
header { padding: 20px 28px; border-bottom: 1px solid var(--line); background: var(--soft); }
header h1 { margin: 0 0 4px; font-size: 20px; }
header .meta { color: var(--info); font-size: 13px; }
main { padding: 8px 28px 48px; max-width: 1400px; }
h2 { margin-top: 32px; font-size: 17px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
nav a { margin-right: 14px; font-size: 13px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th, td { border: 1px solid var(--line); padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: var(--soft); font-weight: 600; }
.badge { display: inline-block; padding: 0 8px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #fff; }
.pass { background: var(--pass); } .warn { background: var(--warn); } .fail { background: var(--fail); } .info { background: var(--info); }
.tag { display: inline-block; padding: 0 6px; border-radius: 4px; font-size: 11px; background: #eaeef2; color: #424a53; margin-right: 4px; }
.tag.bp { background: #fff8c5; } .tag.icon { background: #ddf4ff; } .tag.fig { background: #fbefff; }
details { margin: 2px 0; } summary { cursor: pointer; }
.sec { margin: 4px 0; }
.unit { margin: 2px 0 2px 18px; padding: 2px 6px; border-left: 3px solid var(--line); }
.unit .id { color: var(--info); font-family: Consolas, monospace; font-size: 11px; margin-right: 6px; }
.target { font-size: 12px; color: #0550ae; }
mark.v { background: #fff3b0; } mark.r { background: #ddf4ff; }
span.m-mandatory { color: #0550ae; font-weight: 700; } span.m-prohibition { color: var(--fail); font-weight: 700; }
span.m-recommended { color: #8250df; font-weight: 700; } span.m-permitted { color: var(--warn); font-weight: 700; }
.muted { color: var(--info); } .mono { font-family: Consolas, monospace; font-size: 12px; }
ul.details { margin: 4px 0 0 18px; padding: 0; } ul.details li { margin: 2px 0; }
.legend span { margin-right: 12px; }
"""


def _badge(status: str) -> str:
    return f'<span class="badge {status}">{escape(status.upper())}</span>'


def _checks_table(checks: Iterable[Check]) -> str:
    rows = []
    for c in checks:
        details = ""
        if c.details:
            items = "".join(f"<li>{escape(d)}</li>" for d in c.details)
            details = f'<details><summary>{len(c.details)} detail(s)</summary><ul class="details">{items}</ul></details>'
        rows.append(f"<tr><td>{_badge(c.status)}</td><td><b>{escape(c.name)}</b></td><td>{escape(c.summary)}{details}</td></tr>")
    return "<table><tr><th>Status</th><th>Check</th><th>Result</th></tr>" + "".join(rows) + "</table>"


# ── Highlighting ──────────────────────────────────────────────────────

_MODAL = [
    ("prohibition", r"\b(?:must|shall|may)\s+not\b|\bcannot\b|\bnot\s+permitted\b|\bnot\s+allowed\b|\bnever\b|\bdo\s+not\b|\bprohibited\b"),
    ("recommended", r"\bshould(?:\s+not)?\b|\brecommended\b"),
    ("mandatory", r"\bmust\b|\bshall\b|\brequired\b|\bneeds?\s+to\b|\bha(?:s|ve)\s+to\b|\bmandatory\b"),
    ("permitted", r"\bmay\b|\bcan\b|\bcould\b|\bpermitted\s+to\b|\ballowed\s+to\b|\boptional\b"),
]


def highlight(text: str, values: Iterable[str] = (), roles: Iterable[str] = ()) -> str:
    """Escape *text*, marking protected values, modal words and roles. Overlaps keep the first span."""
    spans: list[tuple[int, int, str, str]] = []
    for raw in sorted(set(values), key=len, reverse=True):
        for m in re.finditer(re.escape(raw), text):
            spans.append((m.start(), m.end(), "mark", "v"))
    for cls, pattern in _MODAL:
        for m in re.finditer(pattern, text, re.I):
            spans.append((m.start(), m.end(), "span", f"m-{cls}"))
    for role in sorted(set(roles), key=len, reverse=True):
        if role:
            for m in re.finditer(rf"(?<![\w-]){re.escape(role)}(?![\w-])", text):
                spans.append((m.start(), m.end(), "mark", "r"))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    out, pos = [], 0
    for start, end, tag, cls in spans:
        if start < pos:
            continue
        out.append(escape(text[pos:start]))
        out.append(f'<{tag} class="{cls}">{escape(text[start:end])}</{tag}>')
        pos = end
    out.append(escape(text[pos:]))
    return "".join(out)


# ── Sections ──────────────────────────────────────────────────────────


def unit_targets(plan: SectionPlan, source: SourceDocument) -> dict[str, str]:
    """unit_id → template section_id (or 'omit' / 'unresolved') as the plan places it."""
    by_id = {s.section_id: s for s in source.sections}
    out: dict[str, str] = {}
    for m in plan.mappings:
        listed = set(m.unit_ids)
        label = m.target_section_id or ("omit" if m.mapping_type == MappingType.OMIT else "unresolved")
        for sid in m.source_section_ids:
            units = [u.unit_id for u in by_id[sid].units] if sid in by_id else []
            covered = [u for u in units if u in listed] if (m.mapping_type == MappingType.SPLIT and listed & set(units)) else units
            for u in covered:
                out[u] = label
    return out


def _plan_section(insp: Inspection) -> str:
    tpl = {t.section_id: t for t in insp.template.model.sections}
    src = {s.section_id: s for s in insp.source.sections}

    def src_label(ids: list[str]) -> str:
        return "<br>".join(f'<span class="mono">{escape(i)}</span> {escape(src[i].heading) if i in src else ""}' for i in ids) or '<span class="muted">—</span>'

    rows = []
    for m in insp.plan.mappings:
        target = tpl.get(m.target_section_id)
        tname = f"<b>{escape(target.key or target.section_id)}</b><br>{escape(target.heading)}" if target else \
            f'<span class="muted">{escape(m.mapping_type.value)}</span>'
        status_cls = {MappingStatus.MAPPED: PASS, MappingStatus.NEEDS_REVIEW: WARN}.get(m.status, INFO)
        units = f"<br><span class='muted'>{len(m.unit_ids)} listed passage(s)</span>" if m.unit_ids else ""
        extra = f"<br><i>{escape(m.justification)}</i>" if m.justification and m.justification != m.reason else ""
        rows.append(
            f"<tr><td>{tname}</td><td>{src_label(m.source_section_ids)}{units}</td>"
            f"<td>{escape(m.mapping_type.value)}</td><td>{_badge(status_cls)} {escape(m.status.value)}</td>"
            f"<td>{escape(m.origin.value)}</td><td>{'' if m.confidence is None else f'{m.confidence:.2f}'}</td>"
            f"<td>{escape(m.reason)}{extra}</td></tr>"
        )
    issues = "".join(
        f"<tr><td>{_badge(FAIL if i.gate else WARN)}</td><td>{escape(i.gate.value if i.gate else '')}</td>"
        f"<td>{escape(i.message)}</td></tr>" for i in insp.plan_report.issues
    ) or '<tr><td colspan="3" class="muted">No validation issues.</td></tr>'
    usage = insp.plan.token_usage
    meta = (f"origin <b>{escape(insp.plan.origin.value)}</b>"
            + (f", model {escape(insp.plan.model or '')}, prompt {escape(insp.plan.prompt_version or '')}, tokens {escape(str(usage))}" if usage else ""))
    return (
        f"<p class='muted'>Plan {meta}. Unit coverage {insp.plan_report.unit_coverage * 100:.0f}%.</p>"
        "<table><tr><th>Template section</th><th>SOP sections</th><th>Type</th><th>Status</th><th>Decided by</th>"
        "<th>Confidence</th><th>Reason</th></tr>" + "".join(rows) + "</table>"
        "<h3>Validation</h3><table><tr><th></th><th>Gate</th><th>Issue</th></tr>" + issues + "</table>"
    )


def _unit_html(u: SourceUnit, values: list[str], roles: list[str], target: str) -> str:
    tags = [f'<span class="tag">{escape(u.unit_type.value)}</span>']
    if u.list_level is not None:
        tags.append(f'<span class="tag">level {u.list_level}{" · " + escape(u.list_number) if u.list_number else ""}</span>')
    if u.is_boilerplate:
        tags.append('<span class="tag bp">boilerplate</span>')
    for a in u.assets:
        tags.append(f'<span class="tag {"icon" if a.kind.value == "icon" else "fig"}">{escape(a.kind.value)}</span>')
    body = highlight(u.text, values, roles)
    if u.table_ref and u.table_ref.cells:
        cells = "".join(f"<td>{highlight(c.text, values, roles)}</td>" for c in u.table_ref.cells)
        head = "".join(f"<th>{escape(h)}</th>" for h in u.table_ref.header_cells) if u.table_ref.header_cells else ""
        body = f'<table>{"<tr>" + head + "</tr>" if head else ""}<tr>{cells}</tr></table>'
    return (f'<div class="unit"><span class="id">{escape(u.unit_id)}</span>{"".join(tags)}'
            f'<span class="target">→ {escape(target)}</span><div>{body}</div></div>')


def _outline_section(insp: Inspection) -> str:
    values: dict[str, list[str]] = {}
    for v in insp.facts.values:
        values.setdefault(v.unit_id, []).append(v.raw)
    roles = list(insp.facts.roles) + list(insp.facts.approved_terms.values())
    targets = unit_targets(insp.plan, insp.source)
    tkey = {t.section_id: (t.key or t.section_id) for t in insp.template.model.sections}
    parts = []
    for s in insp.source.sections:
        depth = max(0, s.level - 1)
        sec_targets = sorted({tkey.get(targets.get(u.unit_id, ""), targets.get(u.unit_id, "—")) for u in s.units})
        units = "".join(_unit_html(u, values.get(u.unit_id, []), roles, tkey.get(targets.get(u.unit_id, ""), targets.get(u.unit_id, "—")))
                        for u in s.units)
        head = (f'<span class="mono">{escape(s.section_id)}</span> <b>{escape((s.number + " ") if s.number else "")}'
                f'{escape(s.heading)}</b> <span class="muted">({len(s.units)} passages)</span> '
                f'<span class="target">→ {escape(", ".join(sec_targets) or "—")}</span>')
        parts.append(f'<details class="sec" style="margin-left:{depth * 20}px"><summary>{head}</summary>{units}</details>')
    legend = ('<p class="legend muted"><span><mark class="v">protected value</mark></span><span><mark class="r">role</mark></span>'
              '<span><span class="m-mandatory">must</span></span><span><span class="m-prohibition">must not</span></span>'
              '<span><span class="m-recommended">should</span></span><span><span class="m-permitted">may</span></span></p>')
    return legend + "".join(parts)


def _facts_section(insp: Inspection) -> str:
    f = insp.facts
    vals = "".join(f"<tr><td>{escape(v.kind.value)}</td><td>{escape(v.raw)}</td><td class='mono'>{escape(v.normalized)}</td>"
                   f"<td class='mono'>{escape(v.unit_id)}</td></tr>" for v in f.values) or "<tr><td colspan=4 class='muted'>none</td></tr>"
    obls = "".join(f"<tr><td>{escape(o.modality.value)}</td><td>{escape(o.actor or '')}</td><td>{escape(o.statement)}</td>"
                   f"<td class='mono'>{escape(o.unit_id)}</td></tr>" for o in f.obligations) or "<tr><td colspan=4 class='muted'>none</td></tr>"
    terms = "".join(f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in f.approved_terms.items()) or "<tr><td colspan=2 class='muted'>none</td></tr>"
    return (
        f"<p><b>Roles:</b> {escape(', '.join(f.roles)) or '<span class=muted>none</span>'}</p>"
        "<details><summary>Abbreviations</summary><table><tr><th>Full term</th><th>Abbreviation</th></tr>" + terms + "</table></details>"
        f"<details open><summary>{len(f.values)} protected values</summary><table><tr><th>Kind</th><th>As written</th>"
        "<th>Compared as</th><th>Passage</th></tr>" + vals + "</table></details>"
        f"<details><summary>{len(f.obligations)} obligations</summary><table><tr><th>Strength</th><th>Actor</th>"
        "<th>Sentence</th><th>Passage</th></tr>" + obls + "</table></details>"
    )


def _template_section(insp: Inspection) -> str:
    t = insp.template
    rows = []
    for s in t.model.sections:
        for slot in s.slots:
            rows.append(f"<tr><td>{escape(s.key or s.section_id)}</td><td>{escape(slot.key or slot.slot_id)}</td>"
                        f"<td>{escape(slot.content_type.value)}</td><td>{'yes' if slot.required else ''}</td>"
                        f"<td>{escape(slot.anchor.kind.value if slot.anchor else 'none')}</td>"
                        f"<td>{escape(slot.instruction[:160])}</td></tr>")
    issues = "".join(f"<li>{escape(i.code)}: {escape(i.message)} ({escape(i.ref or '')})</li>" for i in t.readiness.issues)
    regions = "".join(f"<li>{escape(r.region_id)} [{escape(r.kind)}] {escape(r.section_key or '')}: "
                      f"{escape(r.text[:160])} — suggested <b>{escape(r.suggestion)}</b>"
                      f"{' — decided ' + escape(r.decision) if r.decision else ''}</li>" for r in t.readiness.ambiguous_regions)
    return (
        f"<p>{escape(t.path.name)} — readiness {_badge(PASS if t.readiness.status.value == 'ready' else FAIL)} "
        f"{escape(t.readiness.status.value)}; config: {escape(str(t.config_path) if t.config_path else 'none')}"
        f"{'; normalized' if t.normalized else ''}</p>"
        + (f"<p>Readiness issues:</p><ul>{issues}</ul>" if issues else "")
        + (f"<p>Template regions needing a decision (in the template config):</p><ul>{regions}</ul>" if regions else "")
        + "<table><tr><th>Section</th><th>Slot</th><th>Content type</th><th>Required</th><th>Anchor</th><th>Instruction</th></tr>"
        + "".join(rows) + "</table>"
    )


def _slot_section(insp: Inspection) -> str:
    plan, report = insp.slot_plan, insp.slot_report
    units = {u.unit_id: u for u in insp.source.iter_units()}
    tpl = {t.section_id: t for t in insp.template.model.sections}
    status_cls = {MappingStatus.MAPPED: PASS, MappingStatus.NEEDS_REVIEW: WARN, MappingStatus.SOURCE_CONTENT_NOT_FOUND: WARN}
    parts = []
    for section in plan.sections:
        target = tpl[section.target_section_id]
        slots = {s.slot_id: s for s in target.slots}
        rows = []
        for m in section.slot_mappings:
            slot = slots.get(m.slot_id)
            passages = "".join(
                f'<div><span class="mono">{escape(u)}</span> {escape(units[u].text[:110]) if u in units else ""}'
                f'{"…" if u in units and len(units[u].text) > 110 else ""}</div>' for u in m.source_unit_ids[:4])
            if len(m.source_unit_ids) > 4:
                passages += f"<div class='muted'>+ {len(m.source_unit_ids) - 4} more passage(s)</div>"
            rules = (f"<details><summary>{len(m.rule_ids)}</summary><span class='mono'>{escape(', '.join(m.rule_ids))}</span>"
                     "</details>") if m.rule_ids else "<span class='muted'>—</span>"
            scope = f"<br><span class='tag'>part: {escape(', '.join(m.extraction_scope))}</span>" if m.extraction_scope else ""
            rows.append(
                f"<tr><td><b>{escape(slot.key if slot and slot.key else m.slot_id)}</b><br>"
                f"<span class='muted'>{escape(slot.content_type.value if slot else '')}{' · required' if slot and slot.required else ''}"
                f"</span></td><td>{_badge(status_cls.get(m.status, INFO))} {escape(m.status.value)}</td>"
                f"<td>{escape(m.migration_action.value)}{scope}</td><td>{escape(m.origin.value)}</td>"
                f"<td>{passages or '<span class=muted>—</span>'}</td><td>{rules}</td><td>{escape(m.note or '')}</td></tr>")
        extras = []
        for a in section.callout_assignments:
            extras.append(f"<li>callout <b>{escape(a.kind.value)}</b> ({escape(a.origin.value)}): "
                          f"<span class='mono'>{escape(', '.join(a.unit_ids))}</span> — {escape(a.reason)}</li>")
        for r in section.region_choices:
            extras.append(f"<li>template choice {escape(r.region_id)} answered by <span class='mono'>{escape(', '.join(r.unit_ids))}"
                          f"</span>: <b>{escape(r.choice or '?')}</b></li>")
        extras += [f"<li>{escape(n)}</li>" for n in section.notes]
        parts.append(
            f"<h3>{escape(target.key or target.section_id)} — {escape(target.heading)}</h3>"
            "<table><tr><th>Slot</th><th>Status</th><th>Action</th><th>Decided by</th><th>Passages</th>"
            "<th>GWP rules for the drafter</th><th>Note</th></tr>" + "".join(rows) + "</table>"
            + (f"<ul class='details'>{''.join(extras)}</ul>" if extras else ""))
    issues = "".join(
        f"<tr><td>{_badge(FAIL if i.gate else WARN)}</td><td>{escape(i.gate.value if i.gate else '')}</td>"
        f"<td>{escape(i.message)}</td></tr>" for i in report.issues
    ) or '<tr><td colspan="3" class="muted">No validation issues.</td></tr>'
    usage = plan.token_usage
    meta = (f"origin <b>{escape(plan.origin.value)}</b>, GWP rule set {escape(plan.gwp_guide_id or 'none')}"
            + (f", model {escape(plan.model or '')}, prompt {escape(plan.prompt_version or '')}, tokens {escape(str(usage))}"
               if usage else ""))
    return (f"<p class='muted'>Slot plan {meta}. Unit coverage {report.unit_coverage * 100:.0f}%.</p>" + "".join(parts)
            + "<h3>Validation</h3><table><tr><th></th><th>Gate</th><th>Issue</th></tr>" + issues + "</table>")


_TOKEN = re.compile(r"\{\{ref:([A-Za-z0-9._\-]+)\}\}")


def _claim_text(text: str) -> str:
    return _TOKEN.sub(lambda m: f'<span class="tag">ref → {m.group(1)}</span>', escape(text))


def _draft_section(insp: Inspection) -> str:
    units = {u.unit_id: u for u in insp.source.iter_units()}
    tpl = {t.section_id: t for t in insp.template.model.sections}
    rules = {r.rule_id: r.text for r in insp.rules.rules}
    parts = []
    for draft in insp.drafts:
        target = tpl.get(draft.target_section_id)
        slots = {s.slot_id: s for s in target.slots} if target else {}
        rows = []
        for slot in draft.slots:
            name = slots[slot.slot_id].key if slot.slot_id in slots and slots[slot.slot_id].key else slot.slot_id
            for i, c in enumerate(slot.claims):
                if c.is_gap_marker:
                    text = f"<b class='muted'>[{escape(c.text)}]</b>"
                elif c.kind.value == "heading":
                    text = f"<b>{escape(c.text)}</b>"
                else:
                    indent = "&nbsp;" * 4 * c.list_level + ("• " if c.kind.value == "bullet" else "# " if c.kind.value == "step" else "")
                    text = indent + _claim_text(c.text)
                    changed = any(_norm_text(c.text) != _norm_text(units[u].text) for u in c.source_unit_ids if u in units)
                    if changed and c.source_unit_ids:
                        src = " ".join(units[u].text for u in c.source_unit_ids if u in units)
                        text += f"<details><summary class='muted'>source</summary>{escape(src)}</details>"
                cites = escape(c.source_section_id or ", ".join(c.source_unit_ids))
                used = "".join(f"<span class='tag' title='{escape(rules.get(r, ''))}'>{escape(r)}</span>" for r in c.rule_ids_applied)
                box = f"<span class='tag'>{escape(c.callout_kind.value)}</span>" if c.callout_kind else ""
                rows.append(f"<tr><td>{escape(name) if i == 0 else ''}</td><td class='mono'>{escape(c.claim_id)}</td>"
                            f"<td>{escape(c.kind.value)} {box}</td><td>{text}</td><td class='mono'>{cites}</td><td>{used}</td></tr>")
        notes = "".join(f"<li>{escape(n)}</li>" for n in draft.unresolved_items)
        parts.append(
            f"<details class='sec'><summary><b>{escape(target.key if target and target.key else draft.target_section_id)}</b> "
            f"{escape(target.heading if target else '')} <span class='muted'>({sum(len(s.claims) for s in draft.slots)} claims, "
            f"{escape(draft.origin.value)})</span></summary>"
            "<table><tr><th>Slot</th><th>Claim</th><th>Kind</th><th>Text</th><th>Cites</th><th>GWP rules applied</th></tr>"
            + "".join(rows) + "</table>" + (f"<ul class='details'>{notes}</ul>" if notes else "") + "</details>")
    issues = "".join(
        f"<tr><td>{_badge(FAIL if i.gate else WARN)}</td><td>{escape(i.gate.value if i.gate else '')}</td>"
        f"<td>{escape(i.message)}</td></tr>" for i in insp.draft_report.issues
    ) or '<tr><td colspan="3" class="muted">No issues.</td></tr>'
    return ("<p class='muted'>Reference tokens are resolved to final numbers when the document is assembled (Phase 12). "
            "Rewritten claims show their source under 'source'.</p>" + "".join(parts)
            + f"<h3>Checks (coverage {insp.draft_report.unit_coverage * 100:.0f}%)</h3><table><tr><th></th><th>Gate</th>"
            "<th>Issue</th></tr>" + issues + "</table>")


def _quality_section(insp: Inspection) -> str:
    report = insp.quality
    gates = "".join(f"<tr><td>{_badge(FAIL if n else PASS)}</td><td class='mono'>{escape(g.value)}</td><td>{n}</td></tr>"
                    for g, n in report.gate_counts.items())
    rounds = [e["detail"] for e in insp.events if e["event"] in ("validation", "repair")]
    log = "".join(
        f"<li>validation round {d['round']}: {d['issues']} issue(s), {d['gate_issues']} blocking; critic "
        f"{'read ' + ', '.join(d['critic']['sections']) if d['critic']['sections'] else 'not run'} "
        f"({d['critic']['findings']} finding(s)); repair: {', '.join(d['repair']) or 'none'}</li>" if "round" in d else
        f"<li>repair of {', '.join(d['sections'])}: re-drafted {sum(len(v) for v in d['redrafted'].values())} passage(s)"
        f"{' with the LLM' if d['llm'] else ''}</li>" for d in rounds)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    issues = "".join(
        f"<tr><td>{_badge(FAIL if i.gate else WARN if i.severity.value in ('critical', 'high', 'medium') else INFO)}</td>"
        f"<td>{escape(i.severity.value)}</td><td>{escape(i.gate.value if i.gate else '')}</td><td>{escape(i.source.value)}</td>"
        f"<td>{escape(i.message)}</td></tr>"
        for i in sorted(report.issues, key=lambda i: (i.gate is None, order[i.severity.value])) if not i.resolved
    ) or '<tr><td colspan="5" class="muted">No open issues.</td></tr>'
    units = {u.unit_id: u for u in insp.source.iter_units()}
    risky = "".join(f"<tr><td class='mono'>{escape(u)}</td><td>{escape(', '.join(t.value for t in tags))}</td>"
                    f"<td>{escape((units[u].text if u in units else '')[:160])}</td></tr>"
                    for u, tags in report.high_risk_units.items())
    scores = "".join(f"<li>{escape(k)}: {v:g}</li>" for k, v in sorted(report.soft_scores.items()))
    return (f"<p>The job would end <b>{escape(insp.final_status)}</b>. A gap (<span class='mono'>missing_slot</span>) "
            "waits for a reviewer to add content or accept it as N/A; other gates block completion until fixed or resolved. "
            "Critic findings are advisory; a high one sends its slot to targeted repair (at most 2 rounds).</p>"
            "<table><tr><th></th><th>Hard gate</th><th>Open</th></tr>" + gates + "</table>"
            + (f"<h3>Rounds</h3><ul>{log}</ul>" if log else "")
            + "<h3>Open issues</h3><table><tr><th></th><th>Severity</th><th>Gate</th><th>Found by</th><th>Issue</th></tr>"
            + issues + "</table>"
            + (f"<h3>Soft scores</h3><ul>{scores}</ul>" if scores else "")
            + f"<details><summary>{len(report.high_risk_units)} high-risk passage(s): an issue on one blocks completion</summary>"
            "<table><tr><th>Passage</th><th>Why</th><th>Text</th></tr>" + risky + "</table></details>")


def _document_section(insp: Inspection) -> str:
    report = insp.render
    if report is None:
        return "<p class='muted'>No document was rendered (see the Word document check).</p>"
    link = (f"<p>Review draft: <a href='{escape(insp.document.name)}'>{escape(insp.document.name)}</a> "
            "(gap markers visible, slot content controls in place; references are REF fields to the new numbers).</p>"
            if insp.document else "")
    status = {"filled": PASS, "gap_marker": WARN, "removed_empty": INFO, "removed_accepted_gap": INFO, "no_anchor": FAIL}
    slots = "".join(
        f"<tr><td>{_badge(status.get(s.outcome.value, INFO))}</td><td class='mono'>{escape(s.slot_id)}</td>"
        f"<td>{escape(s.outcome.value.replace('_', ' '))}</td><td>{s.claims}</td><td>{s.tables or ''}</td>"
        f"<td>{s.callout_boxes or ''}</td><td>{escape(s.note or '')}</td></tr>" for s in report.slots)
    regions = "".join(f"<li class='mono'>{escape(r.region_id)}: {escape(r.outcome.value)}"
                      f"{' ' + escape(repr(r.choice)) if r.choice else ''}{' — ' + escape(r.note) if r.note else ''}</li>"
                      for r in report.regions)
    notes = "".join(f"<li><b>Problem:</b> {escape(p)}</li>" for p in report.problems) + "".join(
        f"<li>{escape(w)}</li>" for w in report.warnings)
    return (link + "<table><tr><th></th><th>Slot</th><th>Outcome</th><th>Claims</th><th>Tables</th><th>Callout boxes</th>"
            "<th>Note</th></tr>" + slots + "</table>"
            + (f"<p>Sections removed (optional, no content): {escape(', '.join(report.sections_removed))}</p>"
               if report.sections_removed else "")
            + (f"<h3>Conditional regions</h3><ul>{regions}</ul>" if regions else "")
            + (f"<h3>Problems and warnings</h3><ul>{notes}</ul>" if notes else ""))


def _references_section(insp: Inspection) -> str:
    if insp.number_map is None:
        return "<p class='muted'>Not assembled.</p>"
    chapters = ", ".join(f"{escape(e.target_number)} {escape(e.source_id)}" for e in insp.number_map.entries
                         if e.kind.value == "section" and e.source_id == e.target_section_id)
    removed = (f"<p>Removed (optional, no content): {escape(', '.join(insp.number_map.sections_removed))}</p>"
               if insp.number_map.sections_removed else "")
    status = {"resolved": PASS, "merged": WARN, "unresolved": FAIL}
    rows = "".join(
        f"<tr><td>{_badge(status[r.status.value])}</td><td class='mono'>{escape(r.claim_id)}</td>"
        f"<td>{escape(r.source_phrase)}</td><td>{escape(r.text)}</td><td class='mono'>{escape(r.bookmark or '')}</td>"
        f"<td>{escape(r.note or '')}</td></tr>" for r in (insp.assembled.refs if insp.assembled else []))
    table = ("<table><tr><th></th><th>Claim</th><th>Source says</th><th>New document says</th><th>REF field to</th>"
             f"<th>Note</th></tr>{rows}</table>" if rows else "<p class='muted'>No internal cross-reference.</p>")
    return f"<p>Chapters: {chapters}</p>{removed}{table}"


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", _TOKEN.sub("", text or "")).strip().lower()


def _gwp_section(insp: Inspection) -> str:
    rules = insp.rules
    used = {r for e in insp.events if e["event"] == "slot_planner_llm" for r in e["detail"].get("gwp_rules_in_prompt", [])}
    preview = (f"<p><b>Preview:</b> {insp.gwp_candidates_previewed} candidate rule(s) not yet approved by a reviewer are "
               "treated as approved here. A real migration uses only approved rules.</p>") if insp.gwp_candidates_previewed else ""
    if rules.guide_id == "BASELINE":
        head = ("<p>No GWP for this run: <b>placement mode</b>. Content goes into the slots as written; only the built-in "
                "preservation rules apply.</p>")
    else:
        head = (f"<p>Rules extracted from the guide <b>{escape(rules.guide_id)}</b> v{rules.version} "
                f"({escape(rules.source_file or '')}). Structure and formatting rules go to the slot planner's LLM "
                f"(marked <span class='tag'>prompt</span> when sent in this run); every slot lists the style, preservation "
                "and formatting rules the drafter will apply.</p>")
    rows = "".join(
        f"<tr><td class='mono'>{escape(r.rule_id)}{' <span class=tag>prompt</span>' if r.rule_id in used else ''}</td>"
        f"<td>{escape(r.origin.value)}</td><td>{escape(r.status.value)}</td><td>{escape(r.check.value)}</td>"
        f"<td>{escape(', '.join(c.value for c in r.applies_to_content_types) or 'all')}</td><td>{escape(r.text)}</td></tr>"
        for r in rules.rules)
    return (head + preview + "<details><summary>" + f"{len(rules.rules)} rules" + "</summary><table><tr><th>Rule</th>"
            "<th>Origin</th><th>Status</th><th>Check</th><th>Content types</th><th>Text</th></tr>" + rows + "</table></details>")


def _golden_section(insp: Inspection) -> str:
    if insp.golden is None:
        return "<p class='muted'>No golden expectation exists for this SOP.</p>"
    rows = "".join(f"<tr><td>{escape(r.get('source_heading') or '—')}</td><td>{escape(r.get('target_heading') or '')}</td>"
                   f"<td>{escape(r.get('mapping_type') or '')}</td></tr>" for r in insp.golden.get("section_mapping", []))
    problems = "".join(f"<li>{escape(p)}</li>" for p in insp.golden_problems + insp.golden_slot_problems
                       + insp.golden_draft_problems + insp.missing_preserve)
    problems += "".join(f"<li>reworded by the GWP rewrite (check the meaning): {escape(p)}</li>" for p in insp.draft_paraphrased)
    callouts = "".join(f"<tr><td>{escape(text[:120])}</td><td>{escape(kind)}</td><td>{escape(got)}</td></tr>"
                       for text, kind, got in insp.golden_callouts)
    return ((f"<ul>{problems}</ul>" if problems else "<p>The plans match the golden mapping, slot placements and empty slots.</p>")
            + "<table><tr><th>Golden: SOP heading</th><th>Template section</th><th>Type</th></tr>" + rows + "</table>"
            + ("<h3>Callout suggestions (informational: a reviewer may disagree)</h3><table><tr><th>Passage</th>"
               "<th>Golden suggests</th><th>Plan</th></tr>" + callouts + "</table>" if callouts else ""))


def render(insp: Inspection) -> str:
    status = overall(insp.checks)
    c = insp.completeness
    missing = "".join(f"<li>{escape(x)}</li>" for x in c.missing_context) or "<li class='muted'>Nothing lost.</li>"
    gwp = (f"{escape(insp.rules.guide_id)} v{insp.rules.version}, {len(insp.rules.rules)} rules"
           + (" (candidates previewed as approved)" if insp.gwp_candidates_previewed else "")
           if insp.rules.guide_id != "BASELINE" else "none: placement mode (built-in preservation rules only)")
    artifacts = "".join(f'<li><a href="{escape(p.name)}">{escape(name)}</a></li>' for name, p in sorted(insp.artifacts.items()))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inspection – {escape(insp.sop_path.name)}</title><style>{_CSS}</style></head>
<body><header><h1>{_badge(status)} {escape(insp.sop_path.name)}</h1>
<div class="meta">Template: {escape(insp.template.path.name)} · GWP: {gwp} ·
LLM check: {'on' if insp.llm else 'off'} · Generated {datetime.now():%Y-%m-%d %H:%M}</div>
<nav><a href="#checks">Checks</a><a href="#plan">Section plan</a><a href="#slots">Slot plan</a><a href="#draft">Draft</a><a href="#quality">Quality</a><a href="#document">Document</a><a href="#gwp">GWP rules</a>
<a href="#outline">SOP passages</a>
<a href="#facts">Protected facts</a><a href="#template">Template</a><a href="#text">Text completeness</a>
<a href="#golden">Golden</a><a href="#files">Files</a><a href="../index.html">All SOPs</a></nav></header>
<main>
<h2 id="checks">Checks</h2>{_checks_table(insp.checks)}
<h2 id="plan">Section plan (SOP sections → template sections)</h2>{_plan_section(insp)}
<h2 id="slots">Slot plan (passages → template slots)</h2>{_slot_section(insp)}
<h2 id="draft">Draft (claims per slot)</h2>{_draft_section(insp)}
<h2 id="quality">Quality (validation, critic, repair, gates)</h2>{_quality_section(insp)}
<h2 id="refs">Numbers and cross-references</h2>{_references_section(insp)}
<h2 id="document">Word document (review draft)</h2>{_document_section(insp)}
<h2 id="gwp">GWP rules</h2>{_gwp_section(insp)}
<h2 id="outline">SOP passages, as extracted</h2>{_outline_section(insp)}
<h2 id="facts">Protected facts</h2>{_facts_section(insp)}
<h2 id="template">Template</h2>{_template_section(insp)}
<h2 id="text">Text completeness</h2><p>{c.source_words} source words, {len(c.missing_words)} missing.</p><ul>{missing}</ul>
<h2 id="golden">Golden comparison</h2>{_golden_section(insp)}
<h2 id="files">Files</h2><ul>{artifacts}</ul>
</main></body></html>"""


def render_index(rows: list[tuple[str, str, list[Check]]], errors: list[tuple[str, str]]) -> str:
    """rows: (SOP file name, report href, checks); errors: (SOP file name, error)."""
    names = [c.name for c in rows[0][2]] if rows else []
    head = "".join(f"<th>{escape(n)}</th>" for n in names)
    body = "".join(
        f"<tr><td>{_badge(overall(checks))}</td><td><a href=\"{escape(href)}\">{escape(name)}</a></td>"
        + "".join(f"<td>{_badge(c.status)}</td>" for c in checks) + "</tr>"
        for name, href, checks in rows
    )
    body += "".join(f"<tr><td>{_badge(FAIL)}</td><td>{escape(name)}</td><td colspan='{len(names)}'>error: {escape(err)}</td></tr>"
                    for name, err in errors)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SOP inspection</title><style>{_CSS}</style></head>
<body><header><h1>SOP inspection</h1><div class="meta">{len(rows) + len(errors)} SOP(s) · Generated {datetime.now():%Y-%m-%d %H:%M}</div></header>
<main><table><tr><th>Overall</th><th>SOP</th>{head}</tr>{body}</table>
<p class="muted">PASS = as expected · WARN = look at it (e.g. a mapping to review) · FAIL = must be fixed before migration · INFO = nothing to check.</p>
</main></body></html>"""

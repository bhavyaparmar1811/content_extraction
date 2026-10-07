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


def _golden_section(insp: Inspection) -> str:
    if insp.golden is None:
        return "<p class='muted'>No golden expectation exists for this SOP.</p>"
    rows = "".join(f"<tr><td>{escape(r.get('source_heading') or '—')}</td><td>{escape(r.get('target_heading') or '')}</td>"
                   f"<td>{escape(r.get('mapping_type') or '')}</td></tr>" for r in insp.golden.get("section_mapping", []))
    problems = "".join(f"<li>{escape(p)}</li>" for p in insp.golden_problems + insp.missing_preserve)
    return ((f"<ul>{problems}</ul>" if problems else "<p>The plan matches the golden mapping.</p>")
            + "<table><tr><th>Golden: SOP heading</th><th>Template section</th><th>Type</th></tr>" + rows + "</table>")


def render(insp: Inspection) -> str:
    status = overall(insp.checks)
    c = insp.completeness
    missing = "".join(f"<li>{escape(x)}</li>" for x in c.missing_context) or "<li class='muted'>Nothing lost.</li>"
    gwp = (f"{escape(insp.rules.guide_id)} v{insp.rules.version}, {len(insp.rules.rules)} rules"
           if insp.rules.guide_id != "BASELINE" else "none (built-in preservation rules only)")
    artifacts = "".join(f'<li><a href="{escape(p.name)}">{escape(name)}</a></li>' for name, p in sorted(insp.artifacts.items()))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inspection – {escape(insp.sop_path.name)}</title><style>{_CSS}</style></head>
<body><header><h1>{_badge(status)} {escape(insp.sop_path.name)}</h1>
<div class="meta">Template: {escape(insp.template.path.name)} · GWP: {gwp} ·
LLM check: {'on' if insp.llm else 'off'} · Generated {datetime.now():%Y-%m-%d %H:%M}</div>
<nav><a href="#checks">Checks</a><a href="#plan">Section plan</a><a href="#outline">SOP passages</a>
<a href="#facts">Protected facts</a><a href="#template">Template</a><a href="#text">Text completeness</a>
<a href="#golden">Golden</a><a href="#files">Files</a><a href="../index.html">All SOPs</a></nav></header>
<main>
<h2 id="checks">Checks</h2>{_checks_table(insp.checks)}
<h2 id="plan">Section plan (SOP sections → template sections)</h2>{_plan_section(insp)}
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

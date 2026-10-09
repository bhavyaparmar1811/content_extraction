"""Final export (Phase 12): the finished Word document and the claim → source traceability.

``export_word`` is refused (``ExportBlocked``) unless the job completed: every gate issue, gaps included, is settled
first, so a reviewer has filled each required slot or accepted it as N/A. It then:
1. assembles the document again with the accepted gaps left out (their slots go, and an optional chapter left empty
   goes with them, so the numbers can change);
2. renders it in ``final`` mode (gap slots and their instructions removed, slot content controls unwrapped, REF
   fields, TOC with Word's page numbers when Word is available) and runs the post-render check;
3. saves the ``docx``, ``render_report``, ``number_map`` and ``assembled_draft`` with scope ``final``, and the
   ``traceability``; records one ``gap_removed`` event per accepted gap and an ``export`` event;
4. marks the SOP record with the exported file.

Asking again with nothing changed (same drafts, same accepted gaps) returns the saved export.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.schemas.v2 import (
    ArtifactKind,
    ArtifactRef,
    ClaimKind,
    Gate,
    JobStatus,
    MigrationJob,
    QualityReport,
    RenderMode,
    RenderReport,
    SectionDraft,
    SectionPlan,
    SlotPlan,
    SourceDocument,
    TemplateModel,
    Traceability,
    TraceRow,
)

from .assembly import assemble
from .assembly.numbers import claim_holders
from .render import RenderBlocked, RenderError, render_document
from .render.content import show_refs
from .render.renderer import assembled_ref_text, default_ref_text
from .render.xml import plain_text

FINAL = "final"
EXPORTABLE = (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS)


class ExportBlocked(Exception):
    def __init__(self, code: str, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []


@dataclass
class Export:
    docx: ArtifactRef
    report: RenderReport
    reused: bool = False


def accepted_gaps(report: Optional[QualityReport]) -> list[dict]:
    """The required-slot gaps a reviewer accepted as N/A (resolved ``missing_slot`` issues)."""
    return [{"slot_id": i.slot_id, "issue_id": i.issue_id, "note": i.resolution_note}
            for i in (report.issues if report else []) if i.gate == Gate.MISSING_SLOT and i.resolved and i.slot_id]


def _latest_drafts(artifacts, job: MigrationJob, template: TemplateModel) -> list[SectionDraft]:
    drafts = [artifacts.latest(job, ArtifactKind.SECTION_DRAFT, SectionDraft, t.section_id) for t in template.sections]
    return [d for d in drafts if d is not None]


def build_traceability(job_id: str, drafts: list[SectionDraft], source: SourceDocument, template: TemplateModel,
                       assembled=None, accepted: tuple[str, ...] = ()) -> Traceability:
    holders = claim_holders(template, drafts, accepted)
    units = {u.unit_id: u for u in source.iter_units()}
    sections = {s.section_id: s for s in source.sections}
    ref_text = (assembled_ref_text(assembled, default_ref_text(source), fields=False) if assembled is not None
                else default_ref_text(source))
    rows = []
    for draft in drafts:
        for slot in draft.slots:
            if slot.slot_id in accepted:
                continue
            for claim in slot.claims:
                holder = holders.get(claim.claim_id)
                text = plain_text(show_refs(claim, ref_text))
                base = dict(claim_id=claim.claim_id, target_section_id=draft.target_section_id,
                            target_number=holder.target_number if holder else None, slot_id=slot.slot_id,
                            kind=claim.kind.value, text=text, draft_version=draft.version,
                            draft_origin=draft.origin.value, rule_ids_applied=list(claim.rule_ids_applied),
                            gap_marker=claim.is_gap_marker)
                if claim.kind == ClaimKind.HEADING:
                    section = sections.get(claim.source_section_id)
                    rows.append(TraceRow(**base, source_section_id=claim.source_section_id,
                                         source_section_number=section.number if section else None,
                                         source_section_heading=section.heading if section else None))
                    continue
                if not claim.source_unit_ids:
                    rows.append(TraceRow(**base))
                    continue
                for unit_id in claim.source_unit_ids:
                    unit = units.get(unit_id)
                    section = sections.get(unit.section_id) if unit else None
                    spans = [s for s in claim.spans if s.unit_id == unit_id]
                    part = None
                    if spans and unit is not None:
                        full = " ".join(unit.text.split())
                        part = " … ".join(full[s.start:s.end] for s in spans)
                    rows.append(TraceRow(
                        **base, unit_id=unit_id, part=part,
                        source_section_id=unit.section_id if unit else None,
                        source_section_number=section.number if section else None,
                        source_section_heading=section.heading if section else None,
                        unit_type=unit.unit_type.value if unit else None,
                        page=unit.location.page if unit else None,
                        paragraph_index=unit.location.paragraph_index if unit else None,
                        xml_path=unit.location.xml_path if unit else None,
                    ))
    return Traceability(job_id=job_id, source_file=source.source_file, generated_at=datetime.now(timezone.utc), rows=rows)


def traceability_csv(trace: Traceability) -> str:
    out = io.StringIO()
    fields = list(TraceRow.model_fields)
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for row in trace.rows:
        data = row.model_dump(mode="json")
        data["rule_ids_applied"] = " ".join(data["rule_ids_applied"])
        writer.writerow(data)
    return out.getvalue()


def export_word(job: MigrationJob, store: Any, artifacts: Any, sop_store: Any = None, actor: Optional[str] = None,
                page_counter: Optional[Callable] = None) -> Export:
    if job.status not in EXPORTABLE:
        report = artifacts.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport)
        open_gates = [i.message for i in (report.open_gate_issues if report else [])]
        raise ExportBlocked("EXPORT_BLOCKED", f"Job {job.job_id} is {job.status.value}; the final document is exported once "
                            "the job has completed (every gate issue resolved, gaps filled or accepted)", open_gates)
    source = artifacts.latest(job, ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = artifacts.latest(job, ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    slot_plan = artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan)
    section_plan = artifacts.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan)
    quality = artifacts.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport)
    drafts = _latest_drafts(artifacts, job, template)
    gaps = accepted_gaps(quality)
    accepted = tuple(sorted(g["slot_id"] for g in gaps))
    versions = {d.target_section_id: d.version for d in drafts}

    previous = next((e["detail"] for e in reversed(store.list_events(job.job_id)) if e["event"] == "export"), None)
    last = job.latest_artifact(ArtifactKind.DOCX, FINAL)
    if previous and last and previous.get("draft_versions") == versions and tuple(previous.get("accepted_gaps", [])) == accepted:
        return Export(last, artifacts.latest(job, ArtifactKind.RENDER_REPORT, RenderReport, FINAL), reused=True)

    assembly = assemble(template, drafts, source, section_plan, job_id=job.job_id,
                        version=store.next_artifact_version(job.job_id, ArtifactKind.ASSEMBLED_DRAFT, FINAL),
                        number_map_version=store.next_artifact_version(job.job_id, ArtifactKind.NUMBER_MAP, FINAL),
                        accepted_gaps=accepted)
    work = artifacts.job_dir(job.job_id) / f".export-{store.next_artifact_version(job.job_id, ArtifactKind.DOCX, FINAL)}.docx"
    try:
        report = render_document(template, drafts, source, work, job_id=job.job_id,
                                 version=store.next_artifact_version(job.job_id, ArtifactKind.RENDER_REPORT, FINAL),
                                 slot_plan=slot_plan, mode=RenderMode.FINAL, accepted_gaps=accepted,
                                 page_counter=page_counter, assembled=assembly.document, number_map=assembly.number_map)
        if report.problems:
            artifacts.write(job.job_id, ArtifactKind.RENDER_REPORT, report, scope=FINAL, created_by=actor)
            raise ExportBlocked("EXPORT_CHECK_FAILED", "The final document failed its post-render check", report.problems)
        docx = artifacts.write_file(job.job_id, ArtifactKind.DOCX, work.read_bytes(), ".docx", scope=FINAL, created_by=actor)
    except RenderBlocked as exc:
        raise ExportBlocked("GAPS_UNRESOLVED", str(exc), exc.slot_ids) from exc
    except RenderError as exc:
        raise ExportBlocked("RENDER_FAILED", str(exc)) from exc
    finally:
        work.unlink(missing_ok=True)
    artifacts.write(job.job_id, ArtifactKind.RENDER_REPORT, report, scope=FINAL, created_by=actor)
    artifacts.write(job.job_id, ArtifactKind.NUMBER_MAP, assembly.number_map, scope=FINAL, created_by=actor)
    artifacts.write(job.job_id, ArtifactKind.ASSEMBLED_DRAFT, assembly.document, scope=FINAL, created_by=actor)
    trace = build_traceability(job.job_id, drafts, source, template, assembly.document, accepted)
    artifacts.write(job.job_id, ArtifactKind.TRACEABILITY, trace, created_by=actor)

    for gap in gaps:
        store.add_event(job.job_id, "gap_removed", actor, gap)
    store.add_event(job.job_id, "export", actor, {
        "docx_version": docx.version, "sha256": docx.sha256, "draft_versions": versions, "accepted_gaps": list(accepted),
        "sections_removed": assembly.document.sections_removed, "toc_page_numbers": report.toc_page_numbers,
        "refs": len(assembly.document.refs), "traceability_rows": len(trace.rows), "warnings": len(report.warnings),
    })
    if sop_store is not None:
        sop_store.mark_migrated(job.sop_record_id, job.job_id, docx.path)
    return Export(docx, report)

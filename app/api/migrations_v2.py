"""Migration v2 jobs API (migration_plan.md section 25). Every route requires a signed-in user.

The review draft is rendered to Word since Phase 11 (``GET /{id}/document``). Phase 12 adds the final export
(``GET /{id}/export/word``), the claim → source traceability (``GET /{id}/traceability``, JSON or CSV), and an
audit event with the diff for every reviewer edit.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.auth import require_user
from app.core.exceptions import AppError, NotFoundError
from app.schemas.auth import ApiFieldError
from app.schemas.v2 import (
    ArtifactKind,
    AssembledDocument,
    Claim,
    DraftOrigin,
    JobMode,
    JobStatus,
    MappingOrigin,
    MigrationJob,
    PlanOrigin,
    ProtectedFacts,
    QualityReport,
    RenderReport,
    SectionDraft,
    SectionPlan,
    SectionStatus,
    SlotPlan,
    SourceDocument,
    TemplateModel,
)
from app.services.migration_v2.artifacts import ArtifactCorrupted
from app.services.migration_v2.drafting.checks import draft_report
from app.services.migration_v2.export import ExportBlocked, build_traceability, export_word, traceability_csv
from app.services.migration_v2.render.pages import word_page_counter
from app.services.migration_v2.quality.gates import final_status, gate_counts
from app.services.migration_v2.inputs import InputError
from app.services.migration_v2.orchestrator import JobStateError, Orchestrator
from app.services.migration_v2.plan_checks import section_plan_problems, slot_plan_problems
from app.services.migration_v2.stages import document_rendered

router = APIRouter(prefix="/api/v1/migrations", tags=["Migrations v2"], dependencies=[Depends(require_user)])


class CreateMigrationRequest(BaseModel):
    sop_record_id: int
    template_id: str
    template_version: Optional[int] = None
    gwp_id: Optional[str] = None  # optional; when omitted the job runs without a GWP
    gwp_version: Optional[int] = None
    mode: JobMode = JobMode.REVIEW


# ── Helpers ───────────────────────────────────────────────────────────


def _orchestrator(request: Request) -> Orchestrator:
    orchestrator = getattr(request.app.state, "migration_orchestrator", None)
    if orchestrator is None:
        raise AppError(503, "MIGRATIONS_UNAVAILABLE", "The migration orchestrator is not running")
    return orchestrator


def _claim_diff(before: dict[str, Claim], after: dict[str, Claim]) -> dict[str, list]:
    """What a reviewer's edit changed, claim by claim (the audit trail keeps the before and after text)."""
    def fields(c: Claim) -> dict:
        return c.model_dump(mode="json", include={"text", "kind", "source_unit_ids", "callout_kind", "list_level"})
    changed = [{"claim_id": i, "before": before[i].text, "after": after[i].text,
                "fields": sorted(k for k in fields(after[i]) if fields(after[i])[k] != fields(before[i])[k])}
               for i in after if i in before and fields(after[i]) != fields(before[i])]
    return {"changed": changed,
            "added": [{"claim_id": i, "text": after[i].text} for i in after if i not in before],
            "removed": [{"claim_id": i, "text": before[i].text} for i in before if i not in after]}


def _actor(user: dict) -> Optional[str]:
    return str(user.get("userId")) if user and user.get("userId") is not None else None


def _job(orch: Orchestrator, job_id: str) -> MigrationJob:
    job = orch.store.get_job(job_id)
    if job is None:
        raise NotFoundError(f"Migration job '{job_id}' not found", code="MIGRATION_NOT_FOUND")
    return job


def _view(orch: Orchestrator, job: MigrationJob) -> dict[str, Any]:
    data = job.to_clean_dict()
    data["running"] = orch.is_running(job.job_id)
    return data


def _conflict(exc: JobStateError) -> AppError:
    return AppError(409, exc.code, exc.message)


def _invalid(problems: list[str], code: str) -> AppError:
    return AppError(
        422, code, f"Plan is invalid ({len(problems)} problem(s))",
        field_errors=[ApiFieldError(field="plan", code="invalid", message=p) for p in problems],
    )


def _latest(orch: Orchestrator, job: MigrationJob, kind: ArtifactKind, model, scope: Optional[str] = None):
    try:
        value = orch.artifacts.latest(job, kind, model, scope)
    except ArtifactCorrupted as exc:
        raise AppError(500, "ARTIFACT_CORRUPTED", str(exc)) from exc
    if value is None:
        raise NotFoundError(f"Job '{job.job_id}' has no {kind.value} yet", code="ARTIFACT_NOT_FOUND")
    return value


def _inputs(orch: Orchestrator, job: MigrationJob) -> tuple[SourceDocument, TemplateModel]:
    return (
        _latest(orch, job, ArtifactKind.SOURCE_MODEL, SourceDocument),
        _latest(orch, job, ArtifactKind.TEMPLATE_MODEL, TemplateModel),
    )


# ── Jobs ──────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_migration(body: CreateMigrationRequest, request: Request, user: dict = Depends(require_user)):
    orch = _orchestrator(request)
    try:
        resolved = orch.inputs.resolve(
            body.sop_record_id, body.template_id, body.template_version, body.gwp_id, body.gwp_version
        )
    except InputError as exc:
        raise AppError(exc.status, exc.code, exc.message) from exc
    job = orch.create(resolved, body.mode, _actor(user))
    return _view(orch, job)


@router.get("")
async def list_migrations(
    request: Request,
    status: Optional[JobStatus] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    orch = _orchestrator(request)
    rows = orch.store.list_jobs(status=status.value if status else None, limit=limit, offset=offset)
    for row in rows:
        row["running"] = orch.is_running(row["job_id"])
        row["cancel_requested"] = bool(row["cancel_requested"])
    return rows


@router.get("/{job_id}")
async def get_migration(job_id: str, request: Request):
    orch = _orchestrator(request)
    return _view(orch, _job(orch, job_id))


@router.post("/{job_id}/cancel")
async def cancel_migration(job_id: str, request: Request, user: dict = Depends(require_user)):
    orch = _orchestrator(request)
    _job(orch, job_id)
    try:
        return _view(orch, orch.cancel(job_id, _actor(user)))
    except JobStateError as exc:
        raise _conflict(exc) from exc


@router.post("/{job_id}/retry")
async def retry_migration(
    job_id: str, request: Request, from_stage: Optional[JobStatus] = None, user: dict = Depends(require_user)
):
    orch = _orchestrator(request)
    _job(orch, job_id)
    try:
        return _view(orch, orch.retry(job_id, from_stage, _actor(user)))
    except JobStateError as exc:
        raise _conflict(exc) from exc


@router.get("/{job_id}/audit")
async def get_audit(job_id: str, request: Request):
    orch = _orchestrator(request)
    _job(orch, job_id)
    return orch.store.list_events(job_id)


@router.get("/{job_id}/artifacts")
async def list_artifacts(job_id: str, request: Request):
    orch = _orchestrator(request)
    return [a.model_dump(mode="json", exclude_none=True) for a in _job(orch, job_id).artifacts]


@router.get("/{job_id}/artifacts/{kind}")
async def get_artifact(
    job_id: str, kind: ArtifactKind, request: Request, version: Optional[int] = None, scope: Optional[str] = None
):
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    refs = [a for a in job.artifacts if a.kind == kind and a.scope == scope and (version is None or a.version == version)]
    if not refs:
        raise NotFoundError(f"No {kind.value} artifact for job '{job_id}'", code="ARTIFACT_NOT_FOUND")
    ref = max(refs, key=lambda a: a.version)
    if kind == ArtifactKind.DOCX:
        return _docx_response(orch, job, ref)
    try:
        return orch.artifacts.read(ref)
    except ArtifactCorrupted as exc:
        raise AppError(500, "ARTIFACT_CORRUPTED", str(exc)) from exc


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_response(orch: Orchestrator, job: MigrationJob, ref) -> Response:
    try:
        data = orch.artifacts.read_bytes(ref)
    except ArtifactCorrupted as exc:
        raise AppError(500, "ARTIFACT_CORRUPTED", str(exc)) from exc
    name = f"{job.job_id}_review_v{ref.version}.docx"
    return Response(data, media_type=DOCX_MEDIA_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Rendered document (Phase 11) ──────────────────────────────────────


@router.get("/{job_id}/document")
async def get_document(job_id: str, request: Request, version: Optional[int] = None):
    """The rendered review draft (``.docx``): gap markers visible, slot content controls in place."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    refs = [a for a in job.artifacts if a.kind == ArtifactKind.DOCX and (version is None or a.version == version)]
    if not refs:
        raise NotFoundError(f"Job '{job_id}' has no rendered document yet", code="ARTIFACT_NOT_FOUND")
    return _docx_response(orch, job, max(refs, key=lambda a: a.version))


@router.get("/{job_id}/document/report")
async def get_document_report(job_id: str, request: Request):
    """What the renderer did with each slot and conditional region, warnings for the reviewer, post-render check."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    report = _latest(orch, job, ArtifactKind.RENDER_REPORT, RenderReport)
    return {**report.to_clean_dict(), "ok": report.ok}


# ── Section plan (Level 1) ────────────────────────────────────────────


@router.get("/{job_id}/section-plan")
async def get_section_plan(job_id: str, request: Request):
    orch = _orchestrator(request)
    return _latest(orch, _job(orch, job_id), ArtifactKind.SECTION_PLAN, SectionPlan).to_clean_dict()


@router.patch("/{job_id}/section-plan")
async def patch_section_plan(job_id: str, plan: SectionPlan, request: Request, user: dict = Depends(require_user)):
    """Save a reviewer's edited plan as a new version, and validate it. Only while the job waits for section-plan review.

    Mappings that differ from the previous version are marked ``origin: human``. The response carries
    the validation report under ``validation``; issues with a gate block approval until fixed.
    """
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    if job.status != JobStatus.SECTION_PLAN_REVIEW_PENDING:
        raise AppError(409, "NOT_AWAITING_APPROVAL", f"Job {job_id} is {job.status.value}, not awaiting section-plan review")
    source, template = _inputs(orch, job)
    problems = section_plan_problems(plan, source, template)
    if problems:
        raise _invalid(problems, "SECTION_PLAN_INVALID")
    previous = orch.artifacts.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan)
    before = {_mapping_key(m) for m in previous.mappings} if previous else set()
    mappings = [m if _mapping_key(m) in before else m.model_copy(update={"origin": MappingOrigin.HUMAN}) for m in plan.mappings]
    saved = plan.model_copy(update={
        "job_id": job_id, "version": orch.store.next_artifact_version(job_id, ArtifactKind.SECTION_PLAN),
        "origin": PlanOrigin.HUMAN, "approved_by": None, "mappings": mappings,
    })
    orch.artifacts.write(job_id, ArtifactKind.SECTION_PLAN, saved, created_by=_actor(user))
    after = {_mapping_key(m) for m in plan.mappings}
    orch.store.add_event(job_id, "plan_edited", _actor(user), {
        "plan": "section_plan", "version": saved.version,
        "changed": sorted({m.target_section_id for m in plan.mappings if _mapping_key(m) not in before}),
        "removed": sorted({m.target_section_id for m in (previous.mappings if previous else []) if _mapping_key(m) not in after}),
    })
    report = orch.validate_section_plan(orch.store.get_job(job_id), saved, _actor(user))
    return {**saved.to_clean_dict(), "validation": report.to_clean_dict()}


def _mapping_key(mapping) -> str:
    return mapping.model_dump_json(include={"target_section_id", "source_section_ids", "unit_ids", "mapping_type", "status"})


@router.get("/{job_id}/section-plan/validation")
async def get_section_plan_validation(job_id: str, request: Request):
    """The latest validation report of the section plan (gate issues block approval)."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    return _latest(orch, job, ArtifactKind.QUALITY_REPORT, QualityReport, scope="section_plan").to_clean_dict()


@router.post("/{job_id}/section-plan/approve")
async def approve_section_plan(job_id: str, request: Request, user: dict = Depends(require_user)):
    orch = _orchestrator(request)
    _job(orch, job_id)
    try:
        return _view(orch, orch.approve(job_id, JobStatus.SECTION_PLAN_REVIEW_PENDING, _actor(user)))
    except JobStateError as exc:
        raise _conflict(exc) from exc


# ── Slot plan (Level 2) ───────────────────────────────────────────────


@router.get("/{job_id}/slot-plan")
async def get_slot_plan(job_id: str, request: Request):
    orch = _orchestrator(request)
    return _latest(orch, _job(orch, job_id), ArtifactKind.SLOT_PLAN, SlotPlan).to_clean_dict()


@router.patch("/{job_id}/slot-plan")
async def patch_slot_plan(job_id: str, plan: SlotPlan, request: Request, user: dict = Depends(require_user)):
    """Save a reviewer's edited slot plan as a new version, and validate it. Only while the job waits for slot-plan review.

    Slot mappings that differ from the previous version are marked ``origin: human``. The response carries
    the validation report under ``validation``; issues with a gate block approval until fixed.
    """
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    if job.status != JobStatus.SLOT_PLAN_REVIEW_PENDING:
        raise AppError(409, "NOT_AWAITING_APPROVAL", f"Job {job_id} is {job.status.value}, not awaiting slot-plan review")
    source, template = _inputs(orch, job)
    problems = slot_plan_problems(plan, source, template)
    if problems:
        raise _invalid(problems, "SLOT_PLAN_INVALID")
    previous = orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan)
    before = {_slot_key(s.target_section_id, m) for s in previous.sections for m in s.slot_mappings} if previous else set()
    sections = [
        s.model_copy(update={"slot_mappings": [
            m if _slot_key(s.target_section_id, m) in before else m.model_copy(update={"origin": MappingOrigin.HUMAN})
            for m in s.slot_mappings
        ]})
        for s in plan.sections
    ]
    saved = plan.model_copy(update={
        "job_id": job_id, "version": orch.store.next_artifact_version(job_id, ArtifactKind.SLOT_PLAN),
        "origin": PlanOrigin.HUMAN, "approved_by": None, "sections": sections,
    })
    orch.artifacts.write(job_id, ArtifactKind.SLOT_PLAN, saved, created_by=_actor(user))
    orch.store.add_event(job_id, "plan_edited", _actor(user), {
        "plan": "slot_plan", "version": saved.version,
        "changed": sorted({m.slot_id for s in plan.sections for m in s.slot_mappings
                           if _slot_key(s.target_section_id, m) not in before}),
    })
    report = orch.validate_slot_plan(orch.store.get_job(job_id), saved, _actor(user))
    return {**saved.to_clean_dict(), "validation": report.to_clean_dict()}


def _slot_key(target_section_id: str, mapping) -> str:
    return target_section_id + mapping.model_dump_json(include={"slot_id", "source_unit_ids", "status", "migration_action",
                                                                "extraction_scope"})


@router.get("/{job_id}/slot-plan/validation")
async def get_slot_plan_validation(job_id: str, request: Request):
    """The latest validation report of the slot plan (gate issues block approval)."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    return _latest(orch, job, ArtifactKind.QUALITY_REPORT, QualityReport, scope="slot_plan").to_clean_dict()


@router.post("/{job_id}/slot-plan/approve")
async def approve_slot_plan(job_id: str, request: Request, user: dict = Depends(require_user)):
    orch = _orchestrator(request)
    _job(orch, job_id)
    try:
        return _view(orch, orch.approve(job_id, JobStatus.SLOT_PLAN_REVIEW_PENDING, _actor(user)))
    except JobStateError as exc:
        raise _conflict(exc) from exc


# ── Later phases ──────────────────────────────────────────────────────


@router.get("/{job_id}/validation")
async def get_validation(job_id: str, request: Request):
    """The job's quality report: the hard gates, open issues, high-risk passages and soft scores (Phase 10)."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    ref = job.latest_artifact(ArtifactKind.QUALITY_REPORT)
    if ref is None:
        raise NotFoundError(f"Job '{job_id}' has no quality report yet", code="ARTIFACT_NOT_FOUND")
    return orch.artifacts.read(ref)


class IssueResolution(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


RESOLVABLE_STATUSES = {JobStatus.HUMAN_REVIEW_REQUIRED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.MANUALLY_EDITED}


@router.post("/{job_id}/issues/{issue_id}/resolve")
async def resolve_issue(job_id: str, issue_id: str, body: IssueResolution, request: Request,
                        user: dict = Depends(require_user)):
    """A reviewer resolves one open issue of the quality report, saved as a new report version.

    Resolving a ``missing_slot`` gap accepts the slot as N/A. Resolutions carry over to later validation rounds
    by issue ID. When the last blocking issue of a HUMAN_REVIEW_REQUIRED job is resolved, the job moves on to
    COMPLETED_WITH_WARNINGS or COMPLETED.
    """
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    if orch.is_running(job_id) or job.status not in RESOLVABLE_STATUSES:
        raise AppError(409, "NOT_RESOLVABLE", f"Job {job_id} is {job.status.value}; issues are resolved once it has finished")
    report = _latest(orch, job, ArtifactKind.QUALITY_REPORT, QualityReport)
    issue = next((i for i in report.issues if i.issue_id == issue_id), None)
    if issue is None:
        raise NotFoundError(f"Issue '{issue_id}' is not in the quality report", code="ISSUE_NOT_FOUND")
    if issue.resolved:
        raise AppError(409, "ISSUE_RESOLVED", f"Issue {issue_id} is already resolved")
    actor = _actor(user)
    note = f"{actor}: {body.note.strip()}" if actor else body.note.strip()
    issues = [i.model_copy(update={"resolved": True, "resolution_note": note}) if i.issue_id == issue_id else i
              for i in report.issues]
    saved = report.model_copy(update={"issues": issues, "gate_counts": gate_counts(issues),
                                      "version": orch.store.next_artifact_version(job_id, ArtifactKind.QUALITY_REPORT)})
    orch.artifacts.write(job_id, ArtifactKind.QUALITY_REPORT, saved, created_by=actor)
    orch.store.add_event(job_id, "issue_resolved", actor, {"issue_id": issue_id, "gate": issue.gate.value if issue.gate else None,
                                                           "note": body.note.strip()[:500]})
    if job.status == JobStatus.HUMAN_REVIEW_REQUIRED:
        status = final_status(saved, document_rendered(job, orch.artifacts))
        if status != JobStatus.HUMAN_REVIEW_REQUIRED:
            orch.store.set_status(job_id, status, actor, "review resolved", {"issue_id": issue_id}, expected=job.status)
    return {**saved.to_clean_dict(), "gates_passed": saved.gates_passed, "status": orch.store.get_job(job_id).status.value}


@router.get("/{job_id}/traceability")
async def get_traceability(job_id: str, request: Request, format: str = Query("json", pattern="^(json|csv)$")):
    """Claim → source-unit → source location, for the job's latest drafts (``format=json`` or ``csv``).

    One row per claim and cited passage: the claim's chapter or heading number and text (references resolved), the
    passage, its source section and its location (page, paragraph index). The final export saves the same mapping
    as the ``traceability`` artifact.
    """
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    source, template = _inputs(orch, job)
    drafts = _latest_drafts(orch, job)
    if not drafts:
        raise NotFoundError(f"Job '{job_id}' has no drafts yet", code="ARTIFACT_NOT_FOUND")
    assembled = orch.artifacts.latest(job, ArtifactKind.ASSEMBLED_DRAFT, AssembledDocument)
    if assembled is not None and assembled.draft_versions != {d.target_section_id: d.version for d in drafts}:
        assembled = None  # drafts edited since: references show the source's wording until the next assembly
    trace = build_traceability(job_id, drafts, source, template, assembled)
    if format == "csv":
        return Response(traceability_csv(trace), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{job_id}_traceability.csv"'})
    return trace.to_clean_dict()


@router.get("/{job_id}/export/word")
async def export_word_document(job_id: str, request: Request, user: dict = Depends(require_user)):
    """The final Word document (``final`` mode): accepted gaps removed with their instructions, content controls
    unwrapped, references as REF fields. Refused (409) until the job has completed, i.e. every gate issue is
    resolved and every gap filled or accepted as N/A. Each export is recorded in the audit trail, with every gap it
    removed, and the SOP record is marked with the file; asking again with nothing changed returns the same file.
    """
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    if orch.is_running(job_id):
        raise AppError(409, "JOB_RUNNING", f"Job {job_id} is running; export once it has finished")
    counter = word_page_counter() if getattr(orch.settings, "render_toc_pages", "off") == "word" else None
    try:
        result = await run_in_threadpool(export_word, job, orch.store, orch.artifacts, orch.inputs.sop_store,
                                         _actor(user), counter)
    except ExportBlocked as exc:
        raise AppError(409, exc.code, exc.message,
                       field_errors=[ApiFieldError(field="export", code="blocked", message=d[:500]) for d in exc.details[:20]])
    data = orch.artifacts.read_bytes(result.docx)
    name = f"{job.job_id}_final_v{result.docx.version}.docx"
    return Response(data, media_type=DOCX_MEDIA_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Drafts (Level 3) ──────────────────────────────────────────────────

EDITABLE_DRAFT_STATUSES = {JobStatus.HUMAN_REVIEW_REQUIRED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.MANUALLY_EDITED}


def _latest_drafts(orch: Orchestrator, job: MigrationJob) -> list[SectionDraft]:
    scopes = list(dict.fromkeys(a.scope for a in job.artifacts if a.kind == ArtifactKind.SECTION_DRAFT and a.scope))
    return [orch.artifacts.latest(job, ArtifactKind.SECTION_DRAFT, SectionDraft, scope) for scope in scopes]


@router.get("/{job_id}/drafts")
async def get_drafts(job_id: str, request: Request):
    """The latest draft of every template section."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    drafts = _latest_drafts(orch, job)
    if not drafts:
        raise NotFoundError(f"Job '{job_id}' has no drafts yet", code="ARTIFACT_NOT_FOUND")
    return [d.to_clean_dict() for d in drafts]


@router.get("/{job_id}/drafts/validation")
async def get_draft_validation(job_id: str, request: Request):
    """The latest deterministic checks of the drafts (coverage, citations, references, gaps, preservation)."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    return _latest(orch, job, ArtifactKind.QUALITY_REPORT, QualityReport, scope="drafts").to_clean_dict()


@router.get("/{job_id}/sections/{target_section_id}/draft")
async def get_section_draft(job_id: str, target_section_id: str, request: Request):
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    return _latest(orch, job, ArtifactKind.SECTION_DRAFT, SectionDraft, scope=target_section_id).to_clean_dict()


class SlotContentEdit(BaseModel):
    claims: list[Claim]


@router.patch("/{job_id}/sections/{target_section_id}/slots/{target_slot_id}")
async def patch_slot_content(job_id: str, target_section_id: str, target_slot_id: str, edit: SlotContentEdit,
                             request: Request, user: dict = Depends(require_user)):
    """Replace one slot's claims with a reviewer's version, saved as a new draft version (``origin: human``).

    Allowed once drafting is over and the job is not running. Every claim must still cite source units (or be a
    gap marker or a heading). The response carries the re-run draft checks under ``validation``.
    """
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    if orch.is_running(job_id) or job.status not in EDITABLE_DRAFT_STATUSES:
        raise AppError(409, "DRAFT_NOT_EDITABLE", f"Job {job_id} is {job.status.value}; drafts can be edited once it has finished drafting")
    draft = _latest(orch, job, ArtifactKind.SECTION_DRAFT, SectionDraft, scope=target_section_id)
    if not any(s.slot_id == target_slot_id for s in draft.slots):
        raise NotFoundError(f"Section {target_section_id} has no drafted slot {target_slot_id}", code="SLOT_NOT_FOUND")
    ids = [c.claim_id for c in edit.claims]
    others = {c.claim_id for s in draft.slots if s.slot_id != target_slot_id for c in s.claims}
    clashes = sorted({i for i in ids if ids.count(i) > 1} | (set(ids) & others))
    if clashes:
        raise _invalid([f"duplicate claim_id {i}" for i in clashes], "DRAFT_INVALID")
    before = {c.claim_id: c for s in draft.slots if s.slot_id == target_slot_id for c in s.claims}
    slots = [s.model_copy(update={"claims": edit.claims}) if s.slot_id == target_slot_id else s for s in draft.slots]
    saved = draft.model_copy(update={
        "slots": slots, "origin": DraftOrigin.HUMAN,
        "version": orch.store.next_artifact_version(job_id, ArtifactKind.SECTION_DRAFT, target_section_id),
    })
    orch.artifacts.write(job_id, ArtifactKind.SECTION_DRAFT, saved, scope=target_section_id, created_by=_actor(user))
    orch.store.add_event(job_id, "slot_edited", _actor(user), {
        "section": target_section_id, "slot": target_slot_id, "draft_version": saved.version,
        **_claim_diff(before, {c.claim_id: c for c in edit.claims}),
    })
    orch.store.update_section(job_id, target_section_id, status=SectionStatus.DRAFTED)  # the next validation re-reads it
    if job.status != JobStatus.MANUALLY_EDITED:
        orch.store.set_status(job_id, JobStatus.MANUALLY_EDITED, _actor(user), "draft edited",
                              {"section": target_section_id, "slot": target_slot_id}, expected=job.status)
    job = orch.store.get_job(job_id)
    report = draft_report(
        job_id, orch.store.next_artifact_version(job_id, ArtifactKind.QUALITY_REPORT, "drafts"), _latest_drafts(orch, job),
        orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan), *_inputs(orch, job),
        orch.artifacts.latest(job, ArtifactKind.PROTECTED_FACTS, ProtectedFacts),
    )
    orch.artifacts.write(job_id, ArtifactKind.QUALITY_REPORT, report, scope="drafts", created_by=_actor(user))
    return {**saved.to_clean_dict(), "validation": report.to_clean_dict()}

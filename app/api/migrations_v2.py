"""Migration v2 jobs API (migration_plan.md section 25). Every route requires a signed-in user.

Stages after PARSING are stubs until their phases land; their endpoints
already exist so the review UI can be built against them.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.auth import require_user
from app.core.exceptions import AppError, NotFoundError
from app.schemas.auth import ApiFieldError
from app.schemas.v2 import (
    ArtifactKind,
    JobMode,
    JobStatus,
    MappingOrigin,
    MigrationJob,
    PlanOrigin,
    QualityReport,
    SectionPlan,
    SlotPlan,
    SourceDocument,
    TemplateModel,
)
from app.services.migration_v2.artifacts import ArtifactCorrupted
from app.services.migration_v2.inputs import InputError
from app.services.migration_v2.orchestrator import JobStateError, Orchestrator
from app.services.migration_v2.plan_checks import section_plan_problems, slot_plan_problems

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
    try:
        return orch.artifacts.read(ref)
    except ArtifactCorrupted as exc:
        raise AppError(500, "ARTIFACT_CORRUPTED", str(exc)) from exc


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
    """Save a reviewer's edited slot plan as a new version. Only while the job waits for slot-plan review."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    if job.status != JobStatus.SLOT_PLAN_REVIEW_PENDING:
        raise AppError(409, "NOT_AWAITING_APPROVAL", f"Job {job_id} is {job.status.value}, not awaiting slot-plan review")
    source, template = _inputs(orch, job)
    problems = slot_plan_problems(plan, source, template)
    if problems:
        raise _invalid(problems, "SLOT_PLAN_INVALID")
    saved = plan.model_copy(update={
        "job_id": job_id, "version": orch.store.next_artifact_version(job_id, ArtifactKind.SLOT_PLAN),
        "origin": PlanOrigin.HUMAN, "approved_by": None,
    })
    orch.artifacts.write(job_id, ArtifactKind.SLOT_PLAN, saved, created_by=_actor(user))
    return saved.to_clean_dict()


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
    """The latest quality report (Phase 10)."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    ref = job.latest_artifact(ArtifactKind.QUALITY_REPORT)
    if ref is None:
        raise NotFoundError(f"Job '{job_id}' has no quality report yet", code="ARTIFACT_NOT_FOUND")
    return orch.artifacts.read(ref)


@router.get("/{job_id}/traceability")
async def get_traceability(job_id: str, request: Request):
    """Claim → source-unit traceability (Phase 12)."""
    orch = _orchestrator(request)
    job = _job(orch, job_id)
    ref = job.latest_artifact(ArtifactKind.TRACEABILITY)
    if ref is None:
        raise NotFoundError(f"Job '{job_id}' has no traceability yet", code="ARTIFACT_NOT_FOUND")
    return orch.artifacts.read(ref)


@router.patch("/{job_id}/sections/{target_section_id}/slots/{target_slot_id}")
async def patch_slot_content(job_id: str, target_section_id: str, target_slot_id: str, request: Request):
    """Human edit of drafted slot content (Phases 9 and 13)."""
    orch = _orchestrator(request)
    _job(orch, job_id)
    raise AppError(501, "NOT_IMPLEMENTED", "Editing drafted slot content arrives with the drafter (Phase 9)")

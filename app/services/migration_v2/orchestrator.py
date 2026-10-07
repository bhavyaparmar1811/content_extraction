"""Migration v2 orchestrator: a persisted state machine over ``JobStatus`` (migration_plan.md section 22).

The happy path:

    PENDING → PARSING → PLANNING_SECTIONS → [SECTION_PLAN_REVIEW_PENDING] → PLANNING_SLOTS
      → [SLOT_PLAN_REVIEW_PENDING] → DRAFTING → VALIDATING (⇄ REPAIRING) → ASSEMBLING
      → RECONCILING → QUALITY_REVIEW → COMPLETED / COMPLETED_WITH_WARNINGS / HUMAN_REVIEW_REQUIRED

The review gates (in brackets) apply only in ``mode=review``. All state lives
in the ``MigrationStore``, so nothing is lost on a restart: ``resume_incomplete``
re-queues every job that was mid-stage, and that stage runs again.

Each job runs in its own asyncio task. Cancellation is cooperative: it takes
effect between stages. A server shutdown cancels the tasks but leaves the
statuses alone, so the jobs resume on the next start.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from loguru import logger

from app.schemas.v2 import (
    TERMINAL_STATUSES,
    ArtifactKind,
    JobMode,
    JobStatus,
    MigrationJob,
    QualityReport,
    SectionPlan,
    SlotPlan,
    SourceDocument,
    TemplateModel,
)
from app.stores.migration_store import MigrationStore

from .artifacts import ArtifactWriter
from .inputs import InputResolver, ResolvedInputs
from .planning.section_validator import validate_section_plan
from .stages import SYSTEM_ACTOR, Stage, StageContext, default_stages

# Default next status after each working stage (review gates are applied on top).
NEXT_STATUS: dict[JobStatus, JobStatus] = {
    JobStatus.PENDING: JobStatus.PARSING,
    JobStatus.PARSING: JobStatus.PLANNING_SECTIONS,
    JobStatus.PLANNING_SECTIONS: JobStatus.PLANNING_SLOTS,
    JobStatus.PLANNING_SLOTS: JobStatus.DRAFTING,
    JobStatus.DRAFTING: JobStatus.VALIDATING,
    JobStatus.VALIDATING: JobStatus.ASSEMBLING,
    JobStatus.REPAIRING: JobStatus.VALIDATING,
    JobStatus.ASSEMBLING: JobStatus.RECONCILING,
    JobStatus.RECONCILING: JobStatus.QUALITY_REVIEW,
    JobStatus.QUALITY_REVIEW: JobStatus.COMPLETED,
}

# A working stage followed by a human gate in review mode, and where approval resumes.
REVIEW_GATES: dict[JobStatus, JobStatus] = {
    JobStatus.PLANNING_SECTIONS: JobStatus.SECTION_PLAN_REVIEW_PENDING,
    JobStatus.PLANNING_SLOTS: JobStatus.SLOT_PLAN_REVIEW_PENDING,
}
APPROVE_NEXT: dict[JobStatus, JobStatus] = {
    JobStatus.SECTION_PLAN_REVIEW_PENDING: JobStatus.PLANNING_SLOTS,
    JobStatus.SLOT_PLAN_REVIEW_PENDING: JobStatus.DRAFTING,
}

WORK_STAGES: tuple[JobStatus, ...] = (
    JobStatus.PARSING,
    JobStatus.PLANNING_SECTIONS,
    JobStatus.PLANNING_SLOTS,
    JobStatus.DRAFTING,
    JobStatus.VALIDATING,
    JobStatus.REPAIRING,
    JobStatus.ASSEMBLING,
    JobStatus.RECONCILING,
    JobStatus.QUALITY_REVIEW,
)
WAITING_FOR_HUMAN = frozenset(
    {JobStatus.SECTION_PLAN_REVIEW_PENDING, JobStatus.SLOT_PLAN_REVIEW_PENDING, JobStatus.HUMAN_REVIEW_REQUIRED}
)
RUNNABLE = (JobStatus.PENDING, *WORK_STAGES)

# Artifacts a stage needs; a retry from that stage is refused without them.
STAGE_REQUIRES: dict[JobStatus, tuple[ArtifactKind, ...]] = {
    JobStatus.PLANNING_SECTIONS: (ArtifactKind.SOURCE_MODEL, ArtifactKind.TEMPLATE_MODEL, ArtifactKind.GWP_RULES),
    JobStatus.PLANNING_SLOTS: (ArtifactKind.SECTION_PLAN,),
    JobStatus.DRAFTING: (ArtifactKind.SLOT_PLAN,),
    JobStatus.VALIDATING: (ArtifactKind.SLOT_PLAN,),
    JobStatus.REPAIRING: (ArtifactKind.SLOT_PLAN,),
    JobStatus.ASSEMBLING: (ArtifactKind.SLOT_PLAN,),
    JobStatus.RECONCILING: (ArtifactKind.SLOT_PLAN,),
    JobStatus.QUALITY_REVIEW: (ArtifactKind.SLOT_PLAN,),
}
RETRYABLE_FROM = frozenset(
    {JobStatus.FAILED_TECHNICAL, JobStatus.CANCELLED, JobStatus.HUMAN_REVIEW_REQUIRED,
     JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
)


class JobStateError(Exception):
    """The request does not fit the job's current state (HTTP 409)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def new_job_id() -> str:
    return f"MIG-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:8].upper()}"


class Orchestrator:
    def __init__(
        self,
        store: MigrationStore,
        artifacts: ArtifactWriter,
        inputs: InputResolver,
        settings: Any = None,
        stages: Optional[dict[JobStatus, Stage]] = None,
        rate_limiter: Any = None,
        chain_factory: Any = None,
        max_concurrent_jobs: int = 2,
    ):
        self.store = store
        self.artifacts = artifacts
        self.inputs = inputs
        self.settings = settings
        self.stages: dict[JobStatus, Stage] = {**default_stages(), **(stages or {})}
        self.rate_limiter = rate_limiter
        self.chain_factory = chain_factory
        self._max_concurrent = max_concurrent_jobs
        self._slots: Optional[asyncio.Semaphore] = None
        self._tasks: dict[str, asyncio.Task] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> list[str]:
        """Resume every job a previous process left mid-run."""
        return self.resume_incomplete()

    async def stop(self) -> None:
        """Cancel running tasks. Statuses stay as they are, so the jobs resume on the next start."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def resume_incomplete(self) -> list[str]:
        resumed = []
        for job_id in self.store.job_ids_with_status(list(RUNNABLE)):
            if self.submit(job_id):
                self.store.add_event(job_id, "resumed", SYSTEM_ACTOR)
                resumed.append(job_id)
        if resumed:
            logger.info(f"Migration orchestrator resumed {len(resumed)} job(s): {resumed}")
        return resumed

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def submit(self, job_id: str) -> bool:
        """Start the job's task unless it is already running."""
        if self.is_running(job_id):
            return False
        task = asyncio.create_task(self._run(job_id), name=f"migration:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: self._tasks.pop(jid, None) if self._tasks.get(jid) is t else None)
        return True

    async def wait(self, job_id: str, timeout: float = 30.0) -> MigrationJob:
        """Wait until the job's task finishes (tests and scripts)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.store.get_job(job_id)

    # ── Commands ──────────────────────────────────────────────────────

    def create(self, inputs: ResolvedInputs, mode: JobMode, created_by: Optional[str]) -> MigrationJob:
        job = MigrationJob(
            job_id=new_job_id(),
            sop_record_id=inputs.sop_record_id,
            template_id=inputs.template_id,
            template_version=inputs.template_version,
            gwp_id=inputs.gwp_id,
            gwp_version=inputs.gwp_version,
            mode=mode,
            created_by=created_by,
        )
        job = self.store.create_job(job)
        self.submit(job.job_id)
        return job

    def _job(self, job_id: str) -> MigrationJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def cancel(self, job_id: str, actor: Optional[str]) -> MigrationJob:
        job = self._job(job_id)
        if job.is_terminal:
            raise JobStateError("JOB_FINISHED", f"Job {job_id} is already {job.status.value}")
        if self.is_running(job_id):
            # Cooperative: the run loop stops before the next stage.
            self.store.update_job(job_id, cancel_requested=1)
            self.store.add_event(job_id, "cancel_requested", actor)
        else:
            self.store.set_status(job_id, JobStatus.CANCELLED, actor, "cancelled", expected=job.status)
        return self._job(job_id)

    def approve(self, job_id: str, gate: JobStatus, actor: Optional[str]) -> MigrationJob:
        """Approve the plan the job is waiting on and resume it."""
        job = self._job(job_id)
        if job.status != gate:
            raise JobStateError("NOT_AWAITING_APPROVAL", f"Job {job_id} is {job.status.value}, not {gate.value}")
        kind, model = (
            (ArtifactKind.SECTION_PLAN, SectionPlan)
            if gate == JobStatus.SECTION_PLAN_REVIEW_PENDING
            else (ArtifactKind.SLOT_PLAN, SlotPlan)
        )
        plan = self.artifacts.latest(job, kind, model)
        if plan is not None and gate == JobStatus.SECTION_PLAN_REVIEW_PENDING:
            blocking = self.section_plan_gate_issues(job, plan)
            if blocking:
                raise JobStateError(
                    "PLAN_HAS_GATE_ISSUES",
                    f"The section plan has {len(blocking)} blocking issue(s): " + " | ".join(i.message for i in blocking[:3]),
                )
        if plan is not None:  # record who approved, as a new immutable version
            approved = plan.model_copy(update={"version": self.store.next_artifact_version(job_id, kind), "approved_by": actor})
            self.artifacts.write(job_id, kind, approved, created_by=actor)
        if not self.store.set_status(job_id, APPROVE_NEXT[gate], actor, "approved", {"gate": gate.value}, expected=gate):
            raise JobStateError("NOT_AWAITING_APPROVAL", f"Job {job_id} changed state during approval")
        self.submit(job_id)
        return self._job(job_id)

    def validate_section_plan(self, job: MigrationJob, plan: SectionPlan, actor: Optional[str] = None) -> QualityReport:
        """Validate a section plan against the job's source and template snapshots, and save the report."""
        source = self.artifacts.latest(job, ArtifactKind.SOURCE_MODEL, SourceDocument)
        template = self.artifacts.latest(job, ArtifactKind.TEMPLATE_MODEL, TemplateModel)
        report = validate_section_plan(plan, source, template)
        self.artifacts.write(job.job_id, ArtifactKind.QUALITY_REPORT, report, scope="section_plan", created_by=actor)
        return report

    def section_plan_gate_issues(self, job: MigrationJob, plan: SectionPlan) -> list:
        return self.validate_section_plan(job, plan).open_gate_issues

    def retry(self, job_id: str, from_stage: Optional[JobStatus], actor: Optional[str]) -> MigrationJob:
        job = self._job(job_id)
        if self.is_running(job_id):
            raise JobStateError("JOB_RUNNING", f"Job {job_id} is running")
        if job.status not in RETRYABLE_FROM:
            raise JobStateError("NOT_RETRYABLE", f"Job {job_id} is {job.status.value}; nothing to retry")
        if from_stage is None:
            failed = (self.store.get_job_row(job_id) or {}).get("failed_stage")
            from_stage = JobStatus(failed) if failed else JobStatus.PARSING
        if from_stage not in WORK_STAGES:
            raise JobStateError("INVALID_STAGE", f"{from_stage.value} is not a stage; use one of {[s.value for s in WORK_STAGES]}")
        missing = [k.value for k in STAGE_REQUIRES.get(from_stage, ()) if job.latest_artifact(k) is None]
        if missing:
            raise JobStateError("MISSING_ARTIFACTS", f"Cannot start at {from_stage.value}: no {', '.join(missing)} yet")
        self.store.update_job(job_id, error=None, failed_stage=None, cancel_requested=0)
        self.store.set_status(job_id, from_stage, actor, "retry", {"from_status": job.status.value}, expected=job.status)
        self.submit(job_id)
        return self._job(job_id)

    # ── Run loop ──────────────────────────────────────────────────────

    def _next(self, job: MigrationJob, status: JobStatus) -> JobStatus:
        if job.mode == JobMode.REVIEW and status in REVIEW_GATES:
            return REVIEW_GATES[status]
        return NEXT_STATUS[status]

    async def _run(self, job_id: str) -> None:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self._max_concurrent)
        async with self._slots:
            while True:
                job = self.store.get_job(job_id)
                if job is None or job.status in TERMINAL_STATUSES or job.status in WAITING_FOR_HUMAN:
                    return
                if (self.store.get_job_row(job_id) or {}).get("cancel_requested"):
                    self.store.set_status(job_id, JobStatus.CANCELLED, SYSTEM_ACTOR, "cancelled", expected=job.status)
                    return

                status = job.status
                if status == JobStatus.PENDING:
                    self.store.set_status(job_id, JobStatus.PARSING, SYSTEM_ACTOR, "advanced", expected=status)
                    continue

                stage = self.stages.get(status)
                if stage is None:  # e.g. MANUALLY_EDITED: nothing automatic to do
                    return
                ctx = StageContext(
                    job=job, store=self.store, artifacts=self.artifacts, inputs=self.inputs,
                    settings=self.settings, rate_limiter=self.rate_limiter, chain_factory=self.chain_factory,
                )
                self.store.add_event(job_id, "stage_started", SYSTEM_ACTOR, {"stage": status.value})
                try:
                    override = await stage(ctx)
                except asyncio.CancelledError:
                    self.store.add_event(job_id, "interrupted", SYSTEM_ACTOR, {"stage": status.value})
                    raise
                except Exception as exc:  # technical failure: record it and stop
                    logger.exception(f"Migration {job_id} failed in {status.value}")
                    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
                    self.store.update_job(job_id, error=message, failed_stage=status.value)
                    self.store.set_status(
                        job_id, JobStatus.FAILED_TECHNICAL, SYSTEM_ACTOR, "failed",
                        {"stage": status.value, "error": message}, expected=status,
                    )
                    return

                nxt = override or self._next(job, status)
                self.store.set_status(job_id, nxt, SYSTEM_ACTOR, "advanced", {"stage": status.value}, expected=status)

"""Phase 5: migration job store, artifacts, orchestrator (stubbed stages) and the /api/v1/migrations API."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.auth import require_user
from app.config.settings import Settings, get_settings
from app.schemas.v2 import (
    ArtifactKind,
    GwpRuleSet,
    JobMode,
    JobStatus,
    MigrationJob,
    SectionPlan,
    SectionStatus,
    SlotPlan,
    SourceDocument,
)
from app.services.migration_v2.artifacts import ArtifactCorrupted, ArtifactWriter
from app.services.migration_v2.gwp.baseline import BASELINE_IDS, baseline_rules
from app.services.migration_v2.inputs import InputError, InputResolver
from app.services.migration_v2.orchestrator import JobStateError, Orchestrator
from app.stores.gwp_store import GwpStore
from app.stores.migration_store import MigrationStore
from app.stores.sop_store import SopStore
from app.stores.template_store import TemplateStore

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "migration_v2" / "examples"
STUB_STAGES = {"PLANNING_SLOTS", "DRAFTING", "VALIDATING", "ASSEMBLING", "RECONCILING", "QUALITY_REVIEW"}


# ── Fixtures ──────────────────────────────────────────────────────────


class Env:
    """Temp settings, the three input stores with one SOP and one ready template, and the job store."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.settings = Settings(project_root=tmp_path)
        self.settings.resolve_paths(tmp_path)
        self.settings.ensure_directories()

        self.sop_store = SopStore(tmp_path / "sop.db", self.settings)
        units = tmp_path / "sop_units.json"
        shutil.copy(EXAMPLES / "source_document.json", units)
        self.sop = self.sop_store.upsert_record(job_id="j1", document_uid="SOP-1", units_path=str(units))

        self.template_store = TemplateStore(tmp_path / "tpl.db", self.settings)
        model = tmp_path / "tpl_model.json"
        shutil.copy(EXAMPLES / "template_model.json", model)
        tpl = self.template_store.upsert_record(template_uid="TPL", template_name="Template")
        self.template_store.update_template_model(tpl["id"], str(model), None, "ready")

        self.gwp_store = GwpStore(tmp_path / "gwp.db", self.settings)
        self.store = MigrationStore(tmp_path / "migrations.db", self.settings)
        self.inputs = InputResolver(self.sop_store, self.template_store, self.gwp_store)

    def approved_guide(self, guide_id: str = "GWP") -> dict:
        record = self.gwp_store.create_record(guide_id, "Guide", "pdf")
        rules = self.tmp / f"{guide_id}_rules.json"
        rules.write_text(json.dumps(GwpRuleSet(guide_id=guide_id, version=record["version"], rules=baseline_rules()).to_clean_dict()))
        return self.gwp_store.update_fields(record["id"], rules_path=str(rules), status="approved")

    def orchestrator(self, stages=None) -> Orchestrator:
        return Orchestrator(
            self.store, ArtifactWriter(self.settings.migration_v2_dir, self.store), self.inputs,
            settings=self.settings, stages=stages,
        )

    def create(self, orch: Orchestrator, mode: JobMode = JobMode.AUTO, **kwargs) -> MigrationJob:
        return orch.create(self.inputs.resolve(self.sop["id"], "TPL", **kwargs), mode, "tester")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _events(store: MigrationStore, job_id: str, name: str) -> list[dict]:
    return [e for e in store.list_events(job_id) if e["event"] == name]


# ── Store and artifacts ───────────────────────────────────────────────


def test_store_round_trip_status_guard_sections_and_events(env):
    job = env.store.create_job(MigrationJob(job_id="MIG-1", sop_record_id=1, template_id="TPL", template_version=1))
    assert job.status == JobStatus.PENDING and job.mode == JobMode.REVIEW and job.created_at is not None

    assert env.store.set_status("MIG-1", JobStatus.PARSING, "me", expected=JobStatus.PENDING)
    assert not env.store.set_status("MIG-1", JobStatus.DRAFTING, "me", expected=JobStatus.PENDING)
    assert env.store.get_job("MIG-1").status == JobStatus.PARSING
    with pytest.raises(KeyError):
        env.store.set_status("nope", JobStatus.PARSING)

    env.store.init_sections("MIG-1", ["TGT-2", "TGT-1"])
    env.store.update_section("MIG-1", "TGT-1", status=SectionStatus.DRAFTED, attempts=2, last_error="x")
    env.store.init_sections("MIG-1", ["TGT-2", "TGT-1"])  # a re-run keeps existing rows
    sections = env.store.get_job("MIG-1").sections
    assert [(s.target_section_id, s.status, s.attempts) for s in sections] == [
        ("TGT-2", SectionStatus.PENDING, 0), ("TGT-1", SectionStatus.DRAFTED, 2),
    ]

    with pytest.raises(ValueError):
        env.store.update_job("MIG-1", status="COMPLETED")
    events = env.store.list_events("MIG-1")
    assert [(e["event"], e["from_status"], e["to_status"]) for e in events] == [
        ("created", None, "PENDING"), ("status", "PENDING", "PARSING"),
    ]
    assert [r["job_id"] for r in env.store.list_jobs(status="PARSING")] == ["MIG-1"]


def test_artifacts_are_versioned_hashed_and_verified(env):
    env.store.create_job(MigrationJob(job_id="MIG-1", sop_record_id=1, template_id="TPL", template_version=1))
    writer = ArtifactWriter(env.settings.migration_v2_dir, env.store)
    first = writer.write("MIG-1", ArtifactKind.SECTION_PLAN, SectionPlan(job_id="MIG-1", version=1), created_by="me")
    second = writer.write("MIG-1", ArtifactKind.SECTION_PLAN, {"job_id": "MIG-1", "version": 2})
    scoped = writer.write("MIG-1", ArtifactKind.SECTION_DRAFT, {"x": 1}, scope="TGT-3/a b")
    assert (first.version, second.version, scoped.version) == (1, 2, 1)
    assert Path(first.path).name == "section_plan_v1.json" and Path(scoped.path).name == "section_draft_TGT-3_a_b_v1.json"
    assert first.sha256 != second.sha256

    job = env.store.get_job("MIG-1")
    assert job.latest_artifact(ArtifactKind.SECTION_PLAN).version == 2
    assert writer.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan).version == 2
    assert writer.read(job.latest_artifact(ArtifactKind.SECTION_DRAFT, "TGT-3/a b")) == {"x": 1}

    Path(first.path).write_text('{"tampered": true}')
    with pytest.raises(ArtifactCorrupted):
        writer.read(first)
    assert [e["detail"]["kind"] for e in _events(env.store, "MIG-1", "artifact")] == ["section_plan", "section_plan", "section_draft"]


# ── Input resolution ──────────────────────────────────────────────────


def test_resolve_inputs_refuses_unusable_inputs(env):
    resolved = env.inputs.resolve(env.sop["id"], "TPL")
    assert (resolved.template_id, resolved.template_version, resolved.gwp_id) == ("TPL", 1, None)

    with pytest.raises(InputError) as e:
        env.inputs.resolve(999, "TPL")
    assert (e.value.status, e.value.code) == (404, "SOP_NOT_FOUND")
    with pytest.raises(InputError) as e:
        env.inputs.resolve(env.sop["id"], "TPL", template_version=7)
    assert e.value.code == "TEMPLATE_NOT_FOUND"

    pending = env.template_store.upsert_record(template_uid="TPL2", template_name="Draft")
    env.template_store.update_template_model(pending["id"], str(env.tmp / "tpl_model.json"), None, "needs_review")
    with pytest.raises(InputError) as e:
        env.inputs.resolve(env.sop["id"], "TPL2")
    assert (e.value.status, e.value.code) == (409, "TEMPLATE_NOT_READY")

    guide = env.gwp_store.create_record("G", "Guide", "pdf")
    with pytest.raises(InputError) as e:
        env.inputs.resolve(env.sop["id"], "TPL", gwp_id="G")
    assert e.value.code == "GWP_NOT_APPROVED"
    env.gwp_store.update_fields(guide["id"], status="approved")
    assert env.inputs.resolve(env.sop["id"], "TPL", gwp_id="G").gwp_id == "G"
    assert env.inputs.resolve(env.sop["id"], "TPL").gwp_id is None  # GWP is opt-in, never picked implicitly

    Path(env.sop["units_path"]).unlink()
    with pytest.raises(InputError) as e:
        env.inputs.resolve(env.sop["id"], "TPL")
    assert e.value.code == "SOP_UNITS_MISSING"


# ── Orchestrator ──────────────────────────────────────────────────────


async def test_auto_mode_runs_through_stubbed_stages(env):
    orch = env.orchestrator()
    job = env.create(orch)
    job = await orch.wait(job.job_id)

    assert job.status == JobStatus.COMPLETED_WITH_WARNINGS  # stubs never claim a clean completion
    kinds = {a.kind for a in job.artifacts}
    assert kinds == {ArtifactKind.SOURCE_MODEL, ArtifactKind.TEMPLATE_MODEL, ArtifactKind.GWP_RULES,
                     ArtifactKind.PROTECTED_FACTS, ArtifactKind.SECTION_PLAN, ArtifactKind.QUALITY_REPORT,
                     ArtifactKind.SLOT_PLAN}
    assert job.latest_artifact(ArtifactKind.QUALITY_REPORT, "section_plan") is not None
    assert [s.target_section_id for s in job.sections] == ["TGT-3"]
    assert job.sections[0].status == SectionStatus.SLOT_PLANNED

    rules = orch.artifacts.latest(job, ArtifactKind.GWP_RULES, GwpRuleSet)
    assert {r.rule_id for r in rules.rules} == BASELINE_IDS
    assert _events(env.store, job.job_id, "no_gwp")
    assert {e["detail"]["stage"] for e in _events(env.store, job.job_id, "stage_stub")} == STUB_STAGES

    plan = orch.artifacts.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan)
    assert [(m.target_section_id, m.source_section_ids, m.mapping_type.value) for m in plan.mappings] == [
        ("TGT-3", ["SRC-4.2"], "one_to_one")]  # matched on content: the headings differ
    assert plan.origin.value == "rule" and _events(env.store, job.job_id, "section_planner_rules_only")
    slots = orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan)
    assert [m.slot_id for m in slots.sections[0].slot_mappings] == ["TGT-3-INTRO", "TGT-3-RESPONSIBILITY", "TGT-3-STEPS", "TGT-3-TIMING"]

    transitions = [e["to_status"] for e in env.store.list_events(job.job_id) if e["to_status"]]
    assert transitions == ["PENDING", "PARSING", "PLANNING_SECTIONS", "PLANNING_SLOTS", "DRAFTING", "VALIDATING",
                           "ASSEMBLING", "RECONCILING", "QUALITY_REVIEW", "COMPLETED_WITH_WARNINGS"]


async def test_gwp_is_used_only_when_named(env):
    env.approved_guide("GWP")
    orch = env.orchestrator()

    without = await orch.wait(env.create(orch).job_id)
    assert without.status == JobStatus.COMPLETED_WITH_WARNINGS and without.gwp_id is None
    assert orch.artifacts.latest(without, ArtifactKind.GWP_RULES, GwpRuleSet).guide_id == "BASELINE"

    job = await orch.wait(env.create(orch, gwp_id="GWP").job_id)
    assert (job.gwp_id, job.gwp_version) == ("GWP", 1)
    assert orch.artifacts.latest(job, ArtifactKind.GWP_RULES, GwpRuleSet).guide_id == "GWP"
    assert not _events(env.store, job.job_id, "no_gwp")


async def test_review_mode_pauses_for_both_plans(env):
    orch = env.orchestrator()
    job = await orch.wait(env.create(orch, JobMode.REVIEW).job_id)
    assert job.status == JobStatus.SECTION_PLAN_REVIEW_PENDING and not orch.is_running(job.job_id)

    with pytest.raises(JobStateError):
        orch.approve(job.job_id, JobStatus.SLOT_PLAN_REVIEW_PENDING, "reviewer")
    orch.approve(job.job_id, JobStatus.SECTION_PLAN_REVIEW_PENDING, "reviewer")
    job = await orch.wait(job.job_id)
    assert job.status == JobStatus.SLOT_PLAN_REVIEW_PENDING
    approved = orch.artifacts.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan)
    assert (approved.version, approved.approved_by) == (2, "reviewer")

    orch.approve(job.job_id, JobStatus.SLOT_PLAN_REVIEW_PENDING, "reviewer")
    job = await orch.wait(job.job_id)
    assert job.status == JobStatus.COMPLETED_WITH_WARNINGS
    assert orch.artifacts.latest(job, ArtifactKind.SLOT_PLAN, SlotPlan).approved_by == "reviewer"
    assert [e["actor"] for e in _events(env.store, job.job_id, "approved")] == ["reviewer", "reviewer"]


async def test_failure_records_stage_and_retry_resumes_there(env):
    calls = {"n": 0}

    async def flaky_drafting(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider timeout")

    orch = env.orchestrator({JobStatus.DRAFTING: flaky_drafting})
    job = await orch.wait(env.create(orch).job_id)
    assert job.status == JobStatus.FAILED_TECHNICAL and job.error == "provider timeout"
    assert env.store.get_job_row(job.job_id)["failed_stage"] == "DRAFTING"
    assert _events(env.store, job.job_id, "failed")[0]["detail"] == {"stage": "DRAFTING", "error": "provider timeout"}

    parsing_versions = len([a for a in job.artifacts if a.kind == ArtifactKind.SOURCE_MODEL])
    orch.retry(job.job_id, None, "operator")  # defaults to the failed stage
    job = await orch.wait(job.job_id)
    assert job.status == JobStatus.COMPLETED_WITH_WARNINGS and job.error is None and calls["n"] == 2
    assert len([a for a in job.artifacts if a.kind == ArtifactKind.SOURCE_MODEL]) == parsing_versions  # PARSING not re-run

    with pytest.raises(JobStateError) as e:
        orch.retry(job.job_id, JobStatus.SECTION_PLAN_REVIEW_PENDING, "operator")
    assert e.value.code == "INVALID_STAGE"
    orch.retry(job.job_id, JobStatus.PARSING, "operator")  # a full re-run writes new versions
    job = await orch.wait(job.job_id)
    assert job.latest_artifact(ArtifactKind.SOURCE_MODEL).version == 2


async def test_retry_needs_the_stage_inputs(env):
    async def broken_parsing(ctx):
        raise ValueError("bad units file")

    orch = env.orchestrator({JobStatus.PARSING: broken_parsing})
    job = await orch.wait(env.create(orch).job_id)
    assert job.status == JobStatus.FAILED_TECHNICAL
    with pytest.raises(JobStateError) as e:
        orch.retry(job.job_id, JobStatus.DRAFTING, "operator")
    assert e.value.code == "MISSING_ARTIFACTS"


async def test_cancel_paused_running_and_finished_jobs(env):
    orch = env.orchestrator()
    paused = await orch.wait(env.create(orch, JobMode.REVIEW).job_id)
    assert orch.cancel(paused.job_id, "me").status == JobStatus.CANCELLED  # not running: immediate
    with pytest.raises(JobStateError):
        orch.cancel(paused.job_id, "me")
    with pytest.raises(JobStateError):
        orch.approve(paused.job_id, JobStatus.SECTION_PLAN_REVIEW_PENDING, "me")

    gate = asyncio.Event()

    async def slow_drafting(ctx):
        await gate.wait()

    orch = env.orchestrator({JobStatus.DRAFTING: slow_drafting})
    job = env.create(orch)
    while env.store.get_job(job.job_id).status != JobStatus.DRAFTING:
        await asyncio.sleep(0.01)
    assert orch.cancel(job.job_id, "me").status == JobStatus.DRAFTING  # cooperative: after the stage
    gate.set()
    job = await orch.wait(job.job_id)
    assert job.status == JobStatus.CANCELLED
    assert _events(env.store, job.job_id, "cancel_requested")

    orch.retry(job.job_id, JobStatus.DRAFTING, "me")  # a cancelled job can be restarted
    assert (await orch.wait(job.job_id)).status == JobStatus.COMPLETED_WITH_WARNINGS


async def test_jobs_survive_a_restart(env):
    gate = asyncio.Event()

    async def blocking_validation(ctx):
        await gate.wait()

    first = env.orchestrator({JobStatus.VALIDATING: blocking_validation})
    job = env.create(first)
    while env.store.get_job(job.job_id).status != JobStatus.VALIDATING:
        await asyncio.sleep(0.01)
    await first.stop()  # server shutdown mid-stage
    assert env.store.get_job(job.job_id).status == JobStatus.VALIDATING
    assert _events(env.store, job.job_id, "interrupted")

    second = env.orchestrator()  # a new process on the same database
    assert second.resume_incomplete() == [job.job_id]
    job = await second.wait(job.job_id)
    assert job.status == JobStatus.COMPLETED_WITH_WARNINGS
    assert _events(env.store, job.job_id, "resumed")
    # A job waiting for a human is not resumed.
    paused = await second.wait(env.create(second, JobMode.REVIEW).job_id)
    assert env.orchestrator().resume_incomplete() == []
    assert env.store.get_job(paused.job_id).status == JobStatus.SECTION_PLAN_REVIEW_PENDING


# ── API ───────────────────────────────────────────────────────────────


@pytest.fixture
def api(env):
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: env.settings
    app.dependency_overrides[require_user] = lambda: {"userId": "reviewer-1"}
    with TestClient(app) as client:
        previous = app.state.migration_orchestrator
        orch = env.orchestrator()
        app.state.migration_orchestrator = orch
        try:
            yield client, orch
        finally:
            client.portal.call(orch.stop)
            app.state.migration_orchestrator = previous
            app.dependency_overrides.pop(get_settings, None)
            app.dependency_overrides.pop(require_user, None)


def _wait_for(client, job_id: str, statuses: set[str], timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/migrations/{job_id}").json()
        if job["status"] in statuses and not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {job['status']}")


def test_api_requires_auth(env):
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/migrations").status_code == 401
        assert client.get("/api/v1/gwp").status_code == 401


def test_api_review_flow_end_to_end(api, env):
    client, orch = api

    bad = client.post("/api/v1/migrations", json={"sop_record_id": 999, "template_id": "TPL"})
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "SOP_NOT_FOUND"

    created = client.post("/api/v1/migrations", json={"sop_record_id": env.sop["id"], "template_id": "TPL"})
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    assert job_id.startswith("MIG-") and created.json()["created_by"] == "reviewer-1"

    job = _wait_for(client, job_id, {"SECTION_PLAN_REVIEW_PENDING"})
    assert client.get("/api/v1/migrations", params={"status": "SECTION_PLAN_REVIEW_PENDING"}).json()[0]["job_id"] == job_id
    plan = client.get(f"/api/v1/migrations/{job_id}/section-plan").json()
    assert plan["mappings"][0]["mapping_type"] == "one_to_one"
    assert client.get(f"/api/v1/migrations/{job_id}/section-plan/validation").json()["issues"] == []
    assert client.get(f"/api/v1/migrations/{job_id}/slot-plan").status_code == 404

    plan["mappings"] = [{"target_section_id": "TGT-99", "source_section_ids": ["SRC-4.2"], "mapping_type": "one_to_one"}]
    rejected = client.patch(f"/api/v1/migrations/{job_id}/section-plan", json=plan)
    assert rejected.status_code == 422 and "unknown target section" in rejected.text

    plan["mappings"][0]["target_section_id"] = "TGT-3"
    saved = client.patch(f"/api/v1/migrations/{job_id}/section-plan", json=plan)
    assert saved.status_code == 200, saved.text
    assert (saved.json()["version"], saved.json()["origin"]) == (2, "human")

    assert client.post(f"/api/v1/migrations/{job_id}/slot-plan/approve").status_code == 409
    approved = client.post(f"/api/v1/migrations/{job_id}/section-plan/approve")
    assert approved.status_code == 200, approved.text
    _wait_for(client, job_id, {"SLOT_PLAN_REVIEW_PENDING"})
    assert client.get(f"/api/v1/migrations/{job_id}/section-plan").json()["approved_by"] == "reviewer-1"
    assert client.patch(f"/api/v1/migrations/{job_id}/section-plan", json=plan).status_code == 409

    source = SourceDocument.model_validate(json.loads((EXAMPLES / "source_document.json").read_text()))
    unit_ids = [u.unit_id for u in source.iter_units()]
    slot_plan = client.get(f"/api/v1/migrations/{job_id}/slot-plan").json()
    steps = next(m for m in slot_plan["sections"][0]["slot_mappings"] if m["slot_id"] == "TGT-3-STEPS")
    steps.update({"status": "mapped", "source_unit_ids": unit_ids[:2], "requires_human_review": False})
    steps.pop("note", None)
    slot_plan["sections"][0]["callout_assignments"] = [{"unit_ids": ["NOPE"], "kind": "attention", "reason": "x"}]
    assert client.patch(f"/api/v1/migrations/{job_id}/slot-plan", json=slot_plan).status_code == 422
    slot_plan["sections"][0]["callout_assignments"] = []
    assert client.patch(f"/api/v1/migrations/{job_id}/slot-plan", json=slot_plan).status_code == 200
    assert client.post(f"/api/v1/migrations/{job_id}/slot-plan/approve").status_code == 200
    job = _wait_for(client, job_id, {"COMPLETED_WITH_WARNINGS"})

    artifacts = client.get(f"/api/v1/migrations/{job_id}/artifacts").json()
    assert {(a["kind"], a["version"]) for a in artifacts} >= {("section_plan", 3), ("slot_plan", 3), ("source_model", 1)}
    tpl = client.get(f"/api/v1/migrations/{job_id}/artifacts/template_model").json()
    assert tpl["template_id"] == "TPL-MAIN-GP"
    assert client.get(f"/api/v1/migrations/{job_id}/artifacts/section_plan", params={"version": 1}).json()["version"] == 1
    assert client.get(f"/api/v1/migrations/{job_id}/artifacts/docx").status_code == 404

    audit = client.get(f"/api/v1/migrations/{job_id}/audit").json()
    assert [e["actor"] for e in audit if e["event"] == "approved"] == ["reviewer-1", "reviewer-1"]
    assert audit[-1]["to_status"] == "COMPLETED_WITH_WARNINGS"

    assert client.get(f"/api/v1/migrations/{job_id}/validation").status_code == 404
    assert client.get(f"/api/v1/migrations/{job_id}/traceability").status_code == 404
    assert client.patch(f"/api/v1/migrations/{job_id}/sections/TGT-3/slots/TGT-3-STEPS").status_code == 501
    assert client.post(f"/api/v1/migrations/{job_id}/cancel").status_code == 409

    retried = client.post(f"/api/v1/migrations/{job_id}/retry", params={"from_stage": "PLANNING_SLOTS"})
    assert retried.status_code == 200, retried.text
    _wait_for(client, job_id, {"SLOT_PLAN_REVIEW_PENDING"})  # review mode pauses again
    assert client.post(f"/api/v1/migrations/{job_id}/cancel").json()["status"] == "CANCELLED"
    assert client.get("/api/v1/migrations/MIG-NOPE").status_code == 404


def test_api_refuses_a_template_that_is_not_ready(api, env):
    client, _ = api
    tpl = env.template_store.upsert_record(template_uid="TPL2", template_name="Draft")
    env.template_store.update_template_model(tpl["id"], str(env.tmp / "tpl_model.json"), None, "needs_normalization")
    res = client.post("/api/v1/migrations", json={"sop_record_id": env.sop["id"], "template_id": "TPL2", "mode": "auto"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "TEMPLATE_NOT_READY"

"""Stage functions run by the orchestrator, one per working status.

A stage receives a ``StageContext`` and may return a status to go to instead
of the default next one (VALIDATING → REPAIRING, QUALITY_REVIEW →
HUMAN_REVIEW_REQUIRED...). A stage must be safe to re-run: after a crash or
a retry it runs again and writes the next artifact versions.

Real so far: PARSING (inputs snapshot and protected facts, Phases 5-6),
PLANNING_SECTIONS (section planner, Phase 7) and PLANNING_SLOTS (slot planner,
Phase 8). The others are stubs until their
phase lands; each stub records a ``stage_stub`` event, so a job's audit log
shows what was skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.schemas.v2 import (
    ArtifactKind,
    GwpRuleSet,
    JobStatus,
    MigrationJob,
    PlanOrigin,
    RuleCategory,
    SectionPlan,
    SectionStatus,
    SourceDocument,
    TemplateModel,
)
from app.services.llm.prompts.v2.section_planner import PROMPT_VERSION as SECTION_PLANNER_PROMPT_VERSION
from app.services.llm.prompts.v2.slot_planner import PROMPT_VERSION as SLOT_PLANNER_PROMPT_VERSION
from app.stores.migration_store import MigrationStore

from .artifacts import ArtifactWriter
from .gwp.service import select_rules
from .inputs import InputResolver
from .planning.section_planner import SectionPlanCorrections, SectionPlanner, run_confirm
from .planning.section_validator import validate_section_plan
from .planning.slot_planner import SlotPlanCorrections, SlotPlanner
from .planning.slot_planner import run_confirm as run_slot_confirm
from .planning.slot_validator import validate_slot_plan
from .quality.facts import extract_facts

SYSTEM_ACTOR = "system"


@dataclass
class StageContext:
    job: MigrationJob
    store: MigrationStore
    artifacts: ArtifactWriter
    inputs: InputResolver
    settings: Any = None
    rate_limiter: Any = None
    chain_factory: Any = None

    def write(self, kind: ArtifactKind, payload, scope: Optional[str] = None):
        return self.artifacts.write(self.job.job_id, kind, payload, scope=scope, created_by=SYSTEM_ACTOR)

    def next_version(self, kind: ArtifactKind, scope: Optional[str] = None) -> int:
        return self.store.next_artifact_version(self.job.job_id, kind, scope)

    def latest(self, kind: ArtifactKind, model, scope: Optional[str] = None):
        return self.artifacts.latest(self.job, kind, model, scope)

    def require(self, kind: ArtifactKind, model, scope: Optional[str] = None):
        value = self.latest(kind, model, scope)
        if value is None:
            raise RuntimeError(f"job {self.job.job_id} has no {kind.value} artifact")
        return value

    def stub(self, stage: JobStatus, phase: int) -> None:
        self.store.add_event(
            self.job.job_id, "stage_stub", SYSTEM_ACTOR, {"stage": stage.value, "implemented_in_phase": phase}
        )

    def set_sections(self, status: SectionStatus) -> None:
        for section in self.job.sections:
            self.store.update_section(self.job.job_id, section.target_section_id, status=status)


Stage = Callable[[StageContext], Awaitable[Optional[JobStatus]]]


# ── PARSING: load and snapshot the inputs ─────────────────────────────


async def parsing(ctx: StageContext) -> None:
    source = ctx.inputs.source_document(ctx.job)
    template = ctx.inputs.template_model(ctx.job)
    rules = ctx.inputs.gwp_rules(ctx.job)
    ctx.write(ArtifactKind.SOURCE_MODEL, source)
    ctx.write(ArtifactKind.TEMPLATE_MODEL, template)
    ctx.write(ArtifactKind.GWP_RULES, rules)
    facts = extract_facts(source, ctx.job.job_id)
    ctx.write(ArtifactKind.PROTECTED_FACTS, facts)
    ctx.store.add_event(ctx.job.job_id, "protected_facts", SYSTEM_ACTOR, {
        "values": len(facts.values), "obligations": len(facts.obligations),
        "roles": len(facts.roles), "terms": len(facts.approved_terms),
    })
    ctx.store.init_sections(ctx.job.job_id, [s.section_id for s in template.sections])
    if not ctx.job.gwp_id:
        ctx.store.add_event(ctx.job.job_id, "no_gwp", SYSTEM_ACTOR,
                            {"detail": "no GWP guide selected; the built-in preservation rules still apply"})


# ── Planning ──────────────────────────────────────────────────────────


async def plan_sections(ctx: StageContext) -> None:
    """Level 1 plan: rules propose from names and content; one compact LLM call confirms or corrects (Phase 7)."""
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    settings = ctx.settings
    planner = SectionPlanner(source, template, skip_preamble=getattr(settings, "skip_preamble_migration", True))
    proposal = planner.propose()

    origin, usage, model, prompt_version = PlanOrigin.RULE, {}, None, None
    mode = getattr(settings, "section_planner_llm", "confirm")
    if mode == "confirm" and ctx.chain_factory is not None:
        str_rules = []
        if ctx.job.gwp_id:  # the GWP is optional; its structural rules only refine the check
            rules = ctx.latest(ArtifactKind.GWP_RULES, GwpRuleSet)
            str_rules = select_rules(rules, [RuleCategory.STRUCTURAL]) if rules else []
        prompt = planner.prompt(proposal, getattr(settings, "section_planner_preview_chars", 100), str_rules)
        try:
            chain = ctx.chain_factory.create_structured_planner(SectionPlanCorrections, include_raw=True)
            result = await run_confirm(planner, proposal, chain, prompt, ctx.rate_limiter)
        except Exception as exc:  # the rule plan stands; its doubts stay marked for review
            ctx.store.add_event(ctx.job.job_id, "section_planner_llm_failed", SYSTEM_ACTOR, {"error": str(exc)[:500]})
        else:
            planner.apply(proposal, result.corrections)
            origin, usage = PlanOrigin.LLM, result.usage
            model, prompt_version = ctx.chain_factory.planner_label(), SECTION_PLANNER_PROMPT_VERSION
            ctx.store.add_event(ctx.job.job_id, "section_planner_llm", SYSTEM_ACTOR, {
                "changes": len(result.corrections.changes), "flags": len(result.corrections.flags),
                "dropped_problems": result.problems, "usage": usage,
                "prompt_chars": len(prompt),
            })
    else:
        reason = "section_planner_llm is off" if mode != "confirm" else "no LLM configured"
        ctx.store.add_event(ctx.job.job_id, "section_planner_rules_only", SYSTEM_ACTOR, {"reason": reason})

    plan = planner.build_plan(proposal, ctx.job.job_id, ctx.next_version(ArtifactKind.SECTION_PLAN), origin)
    plan = plan.model_copy(update={"prompt_version": prompt_version, "model": model, "token_usage": usage})
    ctx.write(ArtifactKind.SECTION_PLAN, plan)
    report = validate_section_plan(plan, source, template)
    ctx.write(ArtifactKind.QUALITY_REPORT, report, scope="section_plan")


async def plan_slots(ctx: StageContext) -> None:
    """Level 2 plan inside each approved section mapping: rules place passages, the LLM checks per block (Phase 8).

    The GWP rules come from the job's ``gwp_rules`` artifact, the rule set extracted from the guide the job
    names: its STR and FMT rules go to the LLM, and every slot lists the STY/PRES/FMT rules the drafter applies.
    """
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    section_plan = ctx.require(ArtifactKind.SECTION_PLAN, SectionPlan)
    rules = ctx.latest(ArtifactKind.GWP_RULES, GwpRuleSet)
    settings = ctx.settings
    planner = SlotPlanner(source, template, section_plan, rules)
    proposal = planner.propose()

    origin, usage, model, prompt_version = PlanOrigin.RULE, {}, None, None
    mode = getattr(settings, "slot_planner_llm", "confirm")
    blocks = planner.blocks(proposal, getattr(settings, "slot_planner_block_tokens", 5000),
                            getattr(settings, "slot_planner_preview_chars", 160))
    if mode == "confirm" and ctx.chain_factory is not None and blocks:
        try:
            chain = ctx.chain_factory.create_structured_planner(SlotPlanCorrections, include_raw=True)
            results = []
            for block in blocks:
                prompt = planner.prompt(proposal, block)
                result = await run_slot_confirm(planner, proposal, block, chain, prompt, ctx.rate_limiter)
                results.append((block, prompt, result))
        except Exception as exc:  # the rule plan stands; its doubts stay marked for review
            ctx.store.add_event(ctx.job.job_id, "slot_planner_llm_failed", SYSTEM_ACTOR, {"error": str(exc)[:500]})
        else:
            dropped: list[str] = []
            for block, prompt, result in results:
                dropped += planner.apply(proposal, block, result.corrections)
                for k, v in result.usage.items():
                    usage[k] = usage.get(k, 0) + v
            origin, model, prompt_version = PlanOrigin.LLM, ctx.chain_factory.planner_label(), SLOT_PLANNER_PROMPT_VERSION
            ctx.store.add_event(ctx.job.job_id, "slot_planner_llm", SYSTEM_ACTOR, {
                "blocks": len(blocks),
                "changes": sum(len(r.corrections.changes) for _, _, r in results),
                "callouts": sum(len(r.corrections.callouts) for _, _, r in results),
                "flags": sum(len(r.corrections.flags) for _, _, r in results),
                "dropped": dropped[:20], "usage": usage,
                "prompt_chars": [len(p) for _, p, _ in results],
                "gwp_rules_in_prompt": sorted({r.rule_id for b, _, _ in results for r in planner.block_rules(b)}),
            })
    else:
        reason = ("slot_planner_llm is off" if mode != "confirm" else "no LLM configured" if ctx.chain_factory is None
                  else "nothing to check")
        ctx.store.add_event(ctx.job.job_id, "slot_planner_rules_only", SYSTEM_ACTOR, {"reason": reason})

    plan = planner.build_plan(proposal, ctx.job.job_id, ctx.next_version(ArtifactKind.SLOT_PLAN), origin)
    plan = plan.model_copy(update={"prompt_version": prompt_version, "model": model, "token_usage": usage})
    ctx.write(ArtifactKind.SLOT_PLAN, plan)
    ctx.write(ArtifactKind.QUALITY_REPORT, validate_slot_plan(plan, section_plan, source, template), scope="slot_plan")
    ctx.set_sections(SectionStatus.SLOT_PLANNED)


# ── Stubs (until their phase lands) ───────────────────────────────────


def _stub(stage: JobStatus, phase: int, result: Optional[JobStatus] = None) -> Stage:
    async def run(ctx: StageContext) -> Optional[JobStatus]:
        ctx.stub(stage, phase)
        return result

    run.__name__ = f"{stage.value.lower()}_stub"
    return run


def default_stages() -> dict[JobStatus, Stage]:
    return {
        JobStatus.PARSING: parsing,
        JobStatus.PLANNING_SECTIONS: plan_sections,
        JobStatus.PLANNING_SLOTS: plan_slots,
        JobStatus.DRAFTING: _stub(JobStatus.DRAFTING, 9),
        JobStatus.VALIDATING: _stub(JobStatus.VALIDATING, 10),
        JobStatus.REPAIRING: _stub(JobStatus.REPAIRING, 10),
        JobStatus.ASSEMBLING: _stub(JobStatus.ASSEMBLING, 11),
        JobStatus.RECONCILING: _stub(JobStatus.RECONCILING, 12),
        # Nothing was drafted, so a stubbed run never claims a clean completion.
        JobStatus.QUALITY_REVIEW: _stub(JobStatus.QUALITY_REVIEW, 10, JobStatus.COMPLETED_WITH_WARNINGS),
    }


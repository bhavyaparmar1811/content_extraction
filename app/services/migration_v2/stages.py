"""Stage functions run by the orchestrator, one per working status.

A stage receives a ``StageContext`` and may return a status to go to instead
of the default next one (VALIDATING → REPAIRING, QUALITY_REVIEW →
HUMAN_REVIEW_REQUIRED...). A stage must be safe to re-run: after a crash or
a retry it runs again and writes the next artifact versions.

PARSING (inputs snapshot and protected facts, Phases 5-6), PLANNING_SECTIONS
(section planner, Phase 7), PLANNING_SLOTS (slot planner, Phase 8), DRAFTING
(drafter, Phase 9), VALIDATING, REPAIRING and QUALITY_REVIEW (checks, critic,
repair and gates, Phase 10), then (Phase 12) ASSEMBLING (the whole-document
model: numbers and resolved references), RECONCILING (cross-section checks and
patches, then one more validation round) and RENDERING (the Word review draft,
Phase 11's renderer, and the post-render check).

The orchestrator wraps the chain factory for each stage (``audit.py``), so every
LLM call is written to the job's events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.schemas.v2 import (
    ArtifactKind,
    AssembledDocument,
    NumberMap,
    DraftOrigin,
    GwpRuleSet,
    JobStatus,
    MigrationJob,
    IssueSource,
    PlanOrigin,
    ProtectedFacts,
    QualityReport,
    RenderMode,
    RenderReport,
    RuleCategory,
    SectionDraft,
    Severity,
    SectionPlan,
    SectionStatus,
    SlotPlan,
    SourceDocument,
    TemplateModel,
)
from app.services.llm.prompts.v2.critic import PROMPT_VERSION as CRITIC_PROMPT_VERSION
from app.services.llm.prompts.v2.reconcile import PROMPT_VERSION as RECONCILE_PROMPT_VERSION
from app.services.llm.prompts.v2.section_planner import PROMPT_VERSION as SECTION_PLANNER_PROMPT_VERSION
from app.services.llm.prompts.v2.slot_planner import PROMPT_VERSION as SLOT_PLANNER_PROMPT_VERSION
from app.stores.migration_store import MigrationStore

from .artifacts import ArtifactWriter
from .assembly import Assembly, assemble
from .drafting.checks import draft_report
from .drafting.drafter import Drafter, draft_all
from .gwp.service import select_rules
from .inputs import InputResolver
from .planning.section_planner import SectionPlanCorrections, SectionPlanner, run_confirm
from .planning.section_validator import validate_section_plan
from .planning.slot_planner import SlotPlanCorrections, SlotPlanner
from .planning.slot_planner import run_confirm as run_slot_confirm
from .planning.slot_validator import validate_slot_plan
from .quality.critic import Critic, claim_signature, critique
from .quality.facts import extract_facts
from .quality.gates import carry_resolutions, final_status, quality_report
from .quality.reconcile import reconcile, reconcile_checks
from .quality.repair import claim_locations, issue_sections, repair_sections, repairable
from .quality.validator import validate_drafts
from .render import RenderError, render_document
from .render.pages import word_page_counter

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



# ── Drafting ──────────────────────────────────────────────────────────


async def drafting(ctx: StageContext) -> None:
    """Level 3: one ``SectionDraft`` per template section, from the approved slot plan (Phase 9).

    Placement mode copies; with a GWP the LLM rewrites text passages under the slot's rules from the
    job's ``gwp_rules`` artifact. The deterministic draft checks are saved as a ``quality_report``
    with scope ``drafts`` (Phase 10 adds the critic, repair and gates).
    """
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    section_plan = ctx.require(ArtifactKind.SECTION_PLAN, SectionPlan)
    slot_plan = ctx.require(ArtifactKind.SLOT_PLAN, SlotPlan)
    facts = ctx.require(ArtifactKind.PROTECTED_FACTS, ProtectedFacts)
    rules = ctx.latest(ArtifactKind.GWP_RULES, GwpRuleSet)
    settings = ctx.settings
    drafter = Drafter(source, template, section_plan, slot_plan, facts, rules,
                      max_rules=getattr(settings, "drafter_max_rules", 40),
                      memory_tokens=getattr(settings, "drafter_memory_tokens", 600))
    factory = ctx.chain_factory if getattr(settings, "drafter_llm", "on") == "on" else None
    run = await draft_all(drafter, factory, ctx.rate_limiter, getattr(settings, "drafter_block_tokens", 1500),
                          getattr(settings, "llm_max_concurrent", 3))
    versions = {t.section_id: ctx.next_version(ArtifactKind.SECTION_DRAFT, t.section_id) for t in template.sections}
    drafts = drafter.build_drafts(run, versions, model=factory.planner_label() if factory and run.llm_used else None)
    for draft in drafts:
        ctx.write(ArtifactKind.SECTION_DRAFT, draft, scope=draft.target_section_id)
    report = draft_report(ctx.job.job_id, ctx.next_version(ArtifactKind.QUALITY_REPORT, "drafts"), drafts, slot_plan,
                          source, template, facts)
    ctx.write(ArtifactKind.QUALITY_REPORT, report, scope="drafts")
    ctx.set_sections(SectionStatus.DRAFTED)
    claims = [c for d in drafts for c in d.iter_claims()]
    ctx.store.add_event(ctx.job.job_id, "drafter", SYSTEM_ACTOR, {
        "mode": "gwp" if ctx.job.gwp_id else "placement",
        "llm": run.llm_used, "usage": run.usage,
        "claims": len(claims), "rewritten": sum(bool(c.rule_ids_applied) for c in claims),
        "gaps": sum(c.is_gap_marker for c in claims), "unresolved": sum(len(d.unresolved_items) for d in drafts),
        "gate_issues": len(report.open_gate_issues), "unit_coverage": report.unit_coverage,
    })


# ── Validation, repair and gates (Phase 10) ───────────────────────────


def latest_drafts(ctx: StageContext, template: TemplateModel) -> list[SectionDraft]:
    drafts = [ctx.latest(ArtifactKind.SECTION_DRAFT, SectionDraft, t.section_id) for t in template.sections]
    return [d for d in drafts if d is not None]


_UNCHECKED = (SectionStatus.PENDING, SectionStatus.SLOT_PLANNED, SectionStatus.DRAFTED)


def _previous_draft(ctx: StageContext, draft: SectionDraft) -> Optional[SectionDraft]:
    refs = [a for a in ctx.job.artifacts if a.kind == ArtifactKind.SECTION_DRAFT and a.scope == draft.target_section_id
            and a.version < draft.version]
    ref = max(refs, key=lambda a: a.version, default=None)
    return ctx.artifacts.read_model(ref, SectionDraft) if ref else None


def critic_carry_over(ctx: StageContext, drafts: list[SectionDraft], fresh: set[str],
                      last_round: Optional[QualityReport]) -> tuple[list, dict[str, set[str]]]:
    """The critic reads each claim once. Returns the last round's findings that still stand, and per changed
    section the claim IDs to skip (same text and passages as in the previous draft version).

    A repair rebuilds its section and renumbers the claims, so earlier findings on unchanged claims are moved to
    the claims' new IDs. Re-reading unchanged claims would only invite the model to flag different ones each round.
    """
    old = [i for i in (last_round.issues if last_round else []) if i.source == IssueSource.CRITIC]
    current = {c.claim_id for d in drafts for c in d.iter_claims()}
    carried, skip = [], {}
    for draft in drafts:
        section = draft.target_section_id
        mine = [i for i in old if i.target_section_id == section]
        if section not in fresh:
            carried += [i for i in mine if set(i.claim_ids) <= current]
            continue
        previous = _previous_draft(ctx, draft)
        if previous is None:
            continue
        now = {claim_signature(c): c.claim_id for c in draft.iter_claims()}
        before = {c.claim_id: claim_signature(c) for c in previous.iter_claims()}
        skip[section] = {cid for sig, cid in now.items() if sig in set(before.values())}
        for issue in mine:
            moved = {c: now.get(before.get(c)) for c in issue.claim_ids}
            if all(moved.values()) and set(moved.values()) <= skip[section]:
                message = issue.message
                for a, b in moved.items():
                    message = re.sub(rf"\b{re.escape(a)}\b", b, message)
                carried.append(issue.model_copy(update={"claim_ids": list(moved.values()), "message": message}))
    return carried, skip


async def validating(ctx: StageContext) -> Optional[JobStatus]:
    """One validation round: the deterministic checks on every draft, the critic on the sections drafted or repaired
    since the last round, and the repair decision. A section with an open repairable issue goes to REPAIRING while
    it has attempts left (``repair_max_attempts``), else it needs review.

    Writes a ``quality_report`` with scope ``validation`` per round.
    """
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    slot_plan = ctx.require(ArtifactKind.SLOT_PLAN, SlotPlan)
    facts = ctx.require(ArtifactKind.PROTECTED_FACTS, ProtectedFacts)
    rules = ctx.latest(ArtifactKind.GWP_RULES, GwpRuleSet)
    settings = ctx.settings
    drafts = latest_drafts(ctx, template)
    validation = validate_drafts(drafts, slot_plan, source, template, facts, rules)

    states = {s.target_section_id: s for s in ctx.job.sections}
    fresh = [d for d in drafts if d.target_section_id not in states or states[d.target_section_id].status in _UNCHECKED]
    last_round = ctx.latest(ArtifactKind.QUALITY_REPORT, QualityReport, "validation")
    carried, skip = critic_carry_over(ctx, drafts, {d.target_section_id for d in fresh}, last_round)
    factory = ctx.chain_factory if getattr(settings, "critic_llm", "on") == "on" else None
    critic = Critic(source, template, rules, getattr(settings, "critic_block_tokens", 3000))
    review = await critique(critic, fresh, factory, ctx.rate_limiter, getattr(settings, "llm_max_concurrent", 3), skip)
    issues = carry_resolutions(list({i.issue_id: i for i in validation.issues + carried + review.issues}.values()),
                               ctx.latest(ArtifactKind.QUALITY_REPORT, QualityReport))

    located = claim_locations(drafts)
    needing: set[str] = set()  # an open repairable issue, or a blocking one only a reviewer can settle
    for issue in issues:
        if repairable(issue) or (not issue.resolved and (issue.gate or issue.severity in (Severity.CRITICAL, Severity.HIGH))):
            needing |= issue_sections(issue, located)
    fixable = {s for i in issues if repairable(i) for s in issue_sections(i, located)}
    human = {d.target_section_id for d in drafts if d.origin == DraftOrigin.HUMAN}
    max_attempts = getattr(settings, "repair_max_attempts", 2)
    to_repair = sorted(s for s in fixable if s in states and s not in human and states[s].attempts < max_attempts)
    for section_id in states:
        status = (SectionStatus.REPAIRING if section_id in to_repair else
                  SectionStatus.NEEDS_REVIEW if section_id in needing else SectionStatus.VALIDATED)
        ctx.store.update_section(ctx.job.job_id, section_id, status=status)

    report = QualityReport(job_id=ctx.job.job_id, version=ctx.next_version(ArtifactKind.QUALITY_REPORT, "validation"),
                           issues=issues, unit_coverage=validation.unit_coverage, soft_scores=validation.soft_scores)
    ctx.write(ArtifactKind.QUALITY_REPORT, report, scope="validation")
    ctx.store.add_event(ctx.job.job_id, "validation", SYSTEM_ACTOR, {
        "round": report.version, "issues": len(issues), "gate_issues": len(report.open_gate_issues),
        "critic": {"on": factory is not None, "sections": review.reviewed, "findings": len(review.issues),
                   "failed": review.failed, "usage": review.usage,
                   "prompt_version": CRITIC_PROMPT_VERSION if review.reviewed else None},
        "repair": to_repair, "needs_review": sorted(needing - set(to_repair)),
    })
    return JobStatus.REPAIRING if to_repair else None


async def repairing(ctx: StageContext) -> None:
    """Re-draft only what the last validation round found, in the sections marked REPAIRING; VALIDATING runs next.
    A section's last allowed attempt copies the affected passages from the source instead of re-wording them."""
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    section_plan = ctx.require(ArtifactKind.SECTION_PLAN, SectionPlan)
    slot_plan = ctx.require(ArtifactKind.SLOT_PLAN, SlotPlan)
    facts = ctx.require(ArtifactKind.PROTECTED_FACTS, ProtectedFacts)
    rules = ctx.latest(ArtifactKind.GWP_RULES, GwpRuleSet)
    last_round = ctx.require(ArtifactKind.QUALITY_REPORT, QualityReport, "validation")
    settings = ctx.settings
    sections = {s.target_section_id for s in ctx.job.sections if s.status == SectionStatus.REPAIRING}
    if not sections:
        return
    drafts = latest_drafts(ctx, template)
    located = claim_locations(drafts)
    issues = [i for i in last_round.issues if repairable(i) and issue_sections(i, located) & sections]
    drafter = Drafter(source, template, section_plan, slot_plan, facts, rules,
                      max_rules=getattr(settings, "drafter_max_rules", 40),
                      memory_tokens=getattr(settings, "drafter_memory_tokens", 600))
    factory = ctx.chain_factory if getattr(settings, "drafter_llm", "on") == "on" else None
    versions = {s: ctx.next_version(ArtifactKind.SECTION_DRAFT, s) for s in sections}
    attempts = {s.target_section_id: s.attempts for s in ctx.job.sections}
    last = {s for s in sections if attempts[s] + 1 >= getattr(settings, "repair_max_attempts", 2)}
    result = await repair_sections(drafter, drafts, issues, sections, versions, factory, ctx.rate_limiter,
                                   model=factory.planner_label() if factory else None, verbatim_sections=last)
    for draft in result.drafts:
        ctx.write(ArtifactKind.SECTION_DRAFT, draft, scope=draft.target_section_id)
    for section_id in sections:
        ctx.store.update_section(ctx.job.job_id, section_id, status=SectionStatus.DRAFTED, attempts=attempts[section_id] + 1)
    ctx.store.add_event(ctx.job.job_id, "repair", SYSTEM_ACTOR, {
        "sections": sorted(sections), "last_attempt": sorted(last), "issues": [i.issue_id for i in issues],
        "redrafted": result.redrafted,
        "llm": result.llm_used, "usage": result.usage,
    })


# ── Assembly, reconciliation (Phase 12) and rendering (Phase 11) ──────


def _assemble(ctx: StageContext, template: TemplateModel, drafts: list[SectionDraft], source: SourceDocument,
              accepted_gaps=()) -> Assembly:
    return assemble(template, drafts, source, ctx.latest(ArtifactKind.SECTION_PLAN, SectionPlan), job_id=ctx.job.job_id,
                    version=ctx.next_version(ArtifactKind.ASSEMBLED_DRAFT),
                    number_map_version=ctx.next_version(ArtifactKind.NUMBER_MAP), accepted_gaps=accepted_gaps)


def _write_assembly(ctx: StageContext, assembly: Assembly) -> None:
    ctx.write(ArtifactKind.NUMBER_MAP, assembly.number_map)
    ctx.write(ArtifactKind.ASSEMBLED_DRAFT, assembly.document)


def current_assembly(ctx: StageContext, template: TemplateModel, drafts: list[SectionDraft],
                     source: SourceDocument) -> Assembly:
    """The assembly of *drafts*: the saved one when the drafts are unchanged since, else made and saved again
    (after a reviewer's edit or a reconciliation patch)."""
    assembly = _assemble(ctx, template, drafts, source)
    document = ctx.latest(ArtifactKind.ASSEMBLED_DRAFT, AssembledDocument)
    number_map = ctx.latest(ArtifactKind.NUMBER_MAP, NumberMap)
    if document is not None and number_map is not None and document.draft_versions == assembly.document.draft_versions:
        return Assembly(document, number_map, assembly.issues)
    _write_assembly(ctx, assembly)
    return assembly


async def assembling(ctx: StageContext) -> None:
    """The whole-document model: the latest drafts in target order, the ``NumberMap`` (chapters present, heading
    and passage numbers as Word will show them) and every ``{{ref:...}}`` token resolved (``assembly``)."""
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    drafts = latest_drafts(ctx, template)
    assembly = _assemble(ctx, template, drafts, source)
    _write_assembly(ctx, assembly)
    statuses: dict[str, int] = {}
    for ref in assembly.document.refs:
        statuses[ref.status.value] = statuses.get(ref.status.value, 0) + 1
    ctx.store.add_event(ctx.job.job_id, "assembly", SYSTEM_ACTOR, {
        "sections": assembly.document.section_order, "sections_removed": assembly.document.sections_removed,
        "numbers": len(assembly.number_map.entries), "refs": statuses,
        "issues": [i.message[:200] for i in assembly.issues][:20],
    })


async def reconciling(ctx: StageContext) -> None:
    """Checks over the whole document, then one more validation round (Phase 12).

    Deterministic: abbreviations, role names, numbering and the reference issues. LLM (``reconcile_llm``: ``gwp``
    by default, i.e. when the job rewrites): contradictions, duplicates and terms across sections; a patch of a
    reworded claim is applied only if the whole document still validates (a new draft version, ``origin:
    reconcile``). The round is a ``quality_report`` with scope ``validation``, which QUALITY_REVIEW turns into the
    job's report.
    """
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    slot_plan = ctx.require(ArtifactKind.SLOT_PLAN, SlotPlan)
    facts = ctx.require(ArtifactKind.PROTECTED_FACTS, ProtectedFacts)
    rules = ctx.latest(ArtifactKind.GWP_RULES, GwpRuleSet)
    settings = ctx.settings
    drafts = latest_drafts(ctx, template)

    def validate(candidate: list[SectionDraft]):
        return validate_drafts(candidate, slot_plan, source, template, facts, rules).issues

    mode = getattr(settings, "reconcile_llm", "gwp")
    wanted = mode == "on" or (mode == "gwp" and ctx.job.gwp_id)
    factory = ctx.chain_factory if wanted else None
    run = await reconcile(drafts, template, source, validate, factory, ctx.rate_limiter,
                          getattr(settings, "reconcile_max_chars", 60_000))
    if run.patched:
        for draft in run.patched.values():
            saved = draft.model_copy(update={"version": ctx.next_version(ArtifactKind.SECTION_DRAFT, draft.target_section_id),
                                             "model": factory.planner_label() if factory else None,
                                             "prompt_version": RECONCILE_PROMPT_VERSION})
            ctx.write(ArtifactKind.SECTION_DRAFT, saved, scope=saved.target_section_id)
        ctx.job = ctx.store.get_job(ctx.job.job_id)  # the patched versions are the latest now
        drafts = latest_drafts(ctx, template)
    assembly = current_assembly(ctx, template, drafts, source)

    validation = validate_drafts(drafts, slot_plan, source, template, facts, rules)
    last_round = ctx.latest(ArtifactKind.QUALITY_REPORT, QualityReport, "validation")
    current = {c.claim_id: c.text for d in drafts for c in d.iter_claims()}
    patched_claims = {a["claim_id"] for a in run.applied}
    critic = [i for i in (last_round.issues if last_round else []) if i.source == IssueSource.CRITIC
              and set(i.claim_ids) <= current.keys() and not set(i.claim_ids) & patched_claims]
    issues = (validation.issues + critic + assembly.issues + reconcile_checks(drafts, source, assembly.number_map)
              + run.issues)
    issues = carry_resolutions(list({i.issue_id: i for i in issues}.values()),
                               ctx.latest(ArtifactKind.QUALITY_REPORT, QualityReport))
    report = QualityReport(job_id=ctx.job.job_id, version=ctx.next_version(ArtifactKind.QUALITY_REPORT, "validation"),
                           issues=issues, unit_coverage=validation.unit_coverage, soft_scores=validation.soft_scores)
    ctx.write(ArtifactKind.QUALITY_REPORT, report, scope="validation")
    ctx.store.add_event(ctx.job.job_id, "reconcile", SYSTEM_ACTOR, {
        "round": report.version, "issues": len(issues), "gate_issues": len(report.open_gate_issues),
        "llm": {"on": factory is not None, "skipped": run.skipped, "usage": run.usage,
                "prompt_version": RECONCILE_PROMPT_VERSION if factory is not None and not run.skipped else None},
        "patches_applied": run.applied, "patches_refused": run.rejected,
        "assembled_version": assembly.document.version,
    })


async def rendering(ctx: StageContext) -> None:
    """Render the review draft: the assembled document into a copy of the normalized template (Phase 11).

    Writes a ``docx`` artifact and a ``render_report`` (what happened to each slot and conditional region, warnings
    for the reviewer, and the post-render check, which also compares Word's heading numbers with the number map).
    """
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    slot_plan = ctx.require(ArtifactKind.SLOT_PLAN, SlotPlan)
    drafts = latest_drafts(ctx, template)
    assembly = current_assembly(ctx, template, drafts, source)
    work = ctx.artifacts.job_dir(ctx.job.job_id) / f".render-{ctx.next_version(ArtifactKind.DOCX)}.docx"
    try:
        report = render_document(template, drafts, source, work, job_id=ctx.job.job_id,
                                 version=ctx.next_version(ArtifactKind.RENDER_REPORT), slot_plan=slot_plan,
                                 mode=RenderMode.REVIEW, page_counter=_page_counter(ctx),
                                 assembled=assembly.document, number_map=assembly.number_map)
        ctx.artifacts.write_file(ctx.job.job_id, ArtifactKind.DOCX, work.read_bytes(), ".docx", created_by=SYSTEM_ACTOR)
    except RenderError as exc:  # no usable template file: the drafts stand, the job ends without a document
        ctx.store.add_event(ctx.job.job_id, "render_failed", SYSTEM_ACTOR, {"error": str(exc)[:500]})
        return
    finally:
        work.unlink(missing_ok=True)
    ctx.write(ArtifactKind.RENDER_REPORT, report)
    outcomes: dict[str, int] = {}
    for slot in report.slots:
        outcomes[slot.outcome.value] = outcomes.get(slot.outcome.value, 0) + 1
    ctx.store.add_event(ctx.job.job_id, "render", SYSTEM_ACTOR, {
        "mode": report.mode.value, "slots": outcomes, "sections_removed": report.sections_removed,
        "regions": {r.region_id: r.outcome.value for r in report.regions},
        "warnings": len(report.warnings), "problems": report.problems[:10],
    })


def _page_counter(ctx: StageContext):
    """Word measures the table of contents' page numbers when installed and allowed (``render_toc_pages``)."""
    return word_page_counter() if getattr(ctx.settings, "render_toc_pages", "off") == "word" else None


def document_rendered(job: MigrationJob, artifacts) -> bool:
    """The job has a rendered document that passed the post-render check."""
    if job.latest_artifact(ArtifactKind.DOCX) is None:
        return False
    report = artifacts.latest(job, ArtifactKind.RENDER_REPORT, RenderReport)
    return report is None or report.ok


async def quality_review(ctx: StageContext) -> JobStatus:
    """The hard gates over the last validation round. Writes the job's ``quality_report`` (no scope) and ends the job
    HUMAN_REVIEW_REQUIRED, COMPLETED_WITH_WARNINGS or COMPLETED (``gates.final_status``)."""
    source = ctx.require(ArtifactKind.SOURCE_MODEL, SourceDocument)
    template = ctx.require(ArtifactKind.TEMPLATE_MODEL, TemplateModel)
    facts = ctx.require(ArtifactKind.PROTECTED_FACTS, ProtectedFacts)
    last_round = ctx.require(ArtifactKind.QUALITY_REPORT, QualityReport, "validation")
    drafts = latest_drafts(ctx, template)
    report = quality_report(ctx.job.job_id, ctx.next_version(ArtifactKind.QUALITY_REPORT), last_round, drafts, source,
                            template, facts, ctx.latest(ArtifactKind.QUALITY_REPORT, QualityReport),
                            ctx.latest(ArtifactKind.SLOT_PLAN, SlotPlan))
    ctx.write(ArtifactKind.QUALITY_REPORT, report)
    rendered = document_rendered(ctx.job, ctx.artifacts)
    status = final_status(report, rendered)
    ctx.store.add_event(ctx.job.job_id, "quality_gates", SYSTEM_ACTOR, {
        "status": status.value, "gate_counts": {g.value: n for g, n in report.gate_counts.items() if n},
        "open_issues": sum(not i.resolved for i in report.issues), "high_risk_units": len(report.high_risk_units),
        "rendered": rendered, "soft_scores": report.soft_scores,
    })
    return status


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
        JobStatus.DRAFTING: drafting,
        JobStatus.VALIDATING: validating,
        JobStatus.REPAIRING: repairing,
        JobStatus.ASSEMBLING: assembling,
        JobStatus.RECONCILING: reconciling,
        JobStatus.RENDERING: rendering,
        JobStatus.QUALITY_REVIEW: quality_review,
    }


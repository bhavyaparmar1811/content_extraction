"""Phase 10: validator, GWP style checks, risk classifier, critic, targeted repair, gates, the stage loop and the API.

The phase is done when seeded faults (a dropped number, a weakened obligation, a reordered step) are caught and
repaired, or escalated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.schemas.v2 import (
    ArtifactKind,
    CalloutKind,
    DraftOrigin,
    Gate,
    GwpRule,
    GwpRuleSet,
    IssueCategory,
    IssueSource,
    JobStatus,
    QualityReport,
    RiskTag,
    RuleCategory,
    RuleCheck,
    RuleStatus,
    SectionDraft,
    SectionStatus,
    Severity,
    ValidationIssue,
)
from app.services.migration_v2.drafting.drafter import RewriteOutput, draft_all
from app.services.migration_v2.quality.critic import Critic, CriticFinding, CriticOutput, critique
from app.services.migration_v2.quality.gates import final_status, quality_report
from app.services.migration_v2.quality.repair import repair_sections, repairable
from app.services.migration_v2.quality.risk import classify_text, high_risk_units
from app.services.migration_v2.quality.style import readability, syllables
from app.services.migration_v2.quality.validator import validate_drafts
from tests.test_v2_migration_jobs import resolve_dangling_reference
from tests.test_v2_drafter import FakeChain, FakeFactory, _claims, drafter, echo
from tests.test_v2_slot_planner import gwp, template

REWORD = "This SOP explains"


def reword(unit_id: str, text: str) -> str:
    return text.replace("This SOP describes", REWORD)


async def gwp_drafts(rules=None, transform=reword):
    d, doc, slp = drafter(rules or gwp())
    run = await draft_all(d, FakeFactory(RewriteOutput=FakeChain(echo(transform))))
    return d, doc, slp, d.build_drafts(run, {})


def validate(d, slp, drafts, rules=None):
    return validate_drafts(drafts, slp, d.source, template(), d.facts, rules)


def report_of(validation) -> QualityReport:
    return QualityReport(job_id="J", version=1, issues=validation.issues, unit_coverage=validation.unit_coverage,
                         soft_scores=validation.soft_scores)


def gates(issues):
    return {(i.gate, i.slot_id) for i in issues if i.gate and not i.resolved}


def _find(drafts, slot_id, unit_id):
    return next(c for c in _claims(drafts, slot_id) if c.source_unit_ids == [unit_id])


# ── Seeded faults: caught, then repaired ──────────────────────────────


def seed_faults(drafts):
    """A dropped number, a weakened prohibition and a reordered procedure, all in PROCESS."""
    broken = [x.model_copy(deep=True) for x in drafts]
    process = next(s for x in broken for s in x.slots if s.slot_id == "TGT-6-CONTENT")
    record = next(c for c in process.claims if c.source_unit_ids == ["SRC-6-U001"])
    record.text = record.text.replace("within 5 days", "promptly")
    banned = next(c for c in process.claims if c.source_unit_ids == ["SRC-6.1-U001"])
    banned.text = "The following actions should be avoided:"
    step = next(c for c in process.claims if c.source_unit_ids == ["SRC-6.1-U003"])
    process.claims.remove(step)
    process.claims.insert(0, step)
    return broken


async def test_seeded_faults_are_caught_and_repaired():
    d, doc, slp, drafts = await gwp_drafts()
    assert gates(validate(d, slp, drafts).issues) == set()
    broken = seed_faults(drafts)
    found = validate(d, slp, broken).issues
    kinds = {i.gate for i in found if i.gate}
    assert {Gate.NUMERICAL_CHANGE, Gate.HIGH_RISK_UNRESOLVED, Gate.SEQUENCE_VIOLATION} <= kinds
    order = next(i for i in found if i.gate == Gate.SEQUENCE_VIOLATION)
    assert order.slot_id == "TGT-6-CONTENT" and "out of source order" in order.message

    chain = FakeChain(echo())  # the repair answers with the source wording
    to_fix = [i for i in found if repairable(i)]
    result = await repair_sections(d, broken, to_fix, {"TGT-6"}, {"TGT-6": 2}, FakeFactory(RewriteOutput=chain))
    prompt = chain.calls[0][1].content
    assert "REPAIR:" in prompt and "[SRC-6-U001]" in prompt and "[SRC-6.1-U001]" in prompt
    assert "[SRC-1-U001]" not in prompt and "[SRC-6-U002]" not in prompt  # only the passages the issues point at
    assert "SRC-6-U001: [PRES-" in prompt  # the reasons go with the passages

    repaired = result.drafts[0]
    assert (repaired.target_section_id, repaired.version, repaired.origin) == ("TGT-6", 2, DraftOrigin.REPAIR)
    fixed = [x for x in broken if x.target_section_id != "TGT-6"] + [repaired]
    assert gates(validate(d, slp, fixed).issues) == set()
    process = _claims(fixed, "TGT-6-CONTENT")
    assert "within 5 days" in _find(fixed, "TGT-6-CONTENT", "SRC-6-U001").text
    assert "must not" in _find(fixed, "TGT-6-CONTENT", "SRC-6.1-U001").text
    assert process[0].source_unit_ids == ["SRC-6-U001"]  # back in source order
    assert [c.text for c in process if c.kind.value == "heading"] == ["Assessment", "Closure"]  # derived again


async def test_a_repair_that_breaks_the_text_again_falls_back_to_the_source():
    d, doc, slp, drafts = await gwp_drafts()
    broken = seed_faults(drafts)
    found = [i for i in validate(d, slp, broken).issues if repairable(i)]
    still_wrong = FakeChain(echo(lambda u, t: t.replace("within 5 days", "soon").replace("must not", "should not")))
    result = await repair_sections(d, broken, found, {"TGT-6"}, {"TGT-6": 2}, FakeFactory(RewriteOutput=still_wrong))
    fixed = [x for x in broken if x.target_section_id != "TGT-6"] + result.drafts
    assert gates(validate(d, slp, fixed).issues) == set()
    record = _find(fixed, "TGT-6-CONTENT", "SRC-6-U001")
    assert record.text == "The Process Owner must record every deviation within 5 days (see {{ref:SRC-5}})."
    assert any("the repair failed its checks" in n for n in result.drafts[0].unresolved_items)


async def test_repair_without_an_llm_copies_and_never_touches_gaps():
    d, doc, slp, drafts = await gwp_drafts()
    broken = seed_faults(drafts)
    found = [i for i in validate(d, slp, broken).issues if repairable(i)]
    gap = ValidationIssue(issue_id="ISS-gap", severity=Severity.HIGH, category=IssueCategory.STRUCTURE,
                          gate=Gate.MISSING_SLOT, slot_id="TGT-3-TERMS", message="gap")
    assert not repairable(gap)
    result = await repair_sections(d, broken, found, {"TGT-6"}, {"TGT-6": 2})
    assert not result.llm_used and gates(validate(d, slp, [x for x in broken if x.target_section_id != "TGT-6"]
                                                  + result.drafts).issues) == set()
    assert any("repair without an LLM" in n for n in result.drafts[0].unresolved_items)


# ── Validator: structure, callouts, placeholders ──────────────────────


async def test_validator_flags_callouts_placeholders_and_unknown_slots():
    d, doc, slp = drafter()
    drafts = d.build_drafts(await draft_all(d, None), {})
    broken = [x.model_copy(deep=True) for x in drafts]
    what = next(s for x in broken for s in x.slots if s.slot_id == "TGT-1-WHAT")
    what.claims[0].callout_kind = CalloutKind.ATTENTION  # not assigned to this passage
    what.claims[0].text += " Owner: [TBD]."
    purpose = next(x for x in broken if x.target_section_id == "TGT-1")
    purpose.slots.append(purpose.slots[0].model_copy(update={"slot_id": "TGT-1-NOPE"}))
    issues = validate(d, slp, broken).issues
    messages = " | ".join(i.message for i in issues)
    assert "[callout] C-TGT-1-001 is in a 'attention' callout" in messages
    assert "placeholder '[TBD]', which its source does not have" in messages
    assert (Gate.UNACCOUNTED_SOURCE, "TGT-1-NOPE") in gates(issues)


# ── GWP style checks ──────────────────────────────────────────────────


def style_rules() -> GwpRuleSet:
    base = gwp()

    def rule(rid, kind, **params):
        return GwpRule(rule_id=rid, category=RuleCategory.STYLE, text=f"{kind} rule", check=RuleCheck.DETERMINISTIC,
                       params={"kind": kind, **params}, status=RuleStatus.APPROVED)

    return base.model_copy(update={"rules": base.rules + [
        rule("STY-005", "max_sentence_words", max_words=8),
        rule("STY-009", "forbidden_terms", terms=["ensure"], prefer=["confirm"]),
        rule("STY-014", "forbidden_terms", terms=["may", "recommend"]),
        rule("STY-021", "passive_ratio", max_ratio=0.1),
        rule("STY-019", "readability", flesch_reading_ease_min=90),
    ]})


async def test_style_checks_are_soft_and_only_on_rewritten_slots():
    rules = style_rules()
    d, doc, slp, drafts = await gwp_drafts(rules, lambda u, t: t.replace("are recorded", "are recorded to ensure quality"))
    issues = [i for i in validate(d, slp, drafts, rules).issues if i.message.startswith("[GWP]")]
    assert issues and all(i.severity == Severity.LOW and i.gate is None for i in issues)
    text = " | ".join(i.message for i in issues)
    assert "STY-009 uses 'ensure'; prefer confirm" in text and "STY-005 1 sentence(s) over 8 words" in text
    assert "STY-021" in text and "STY-019" in text
    assert "SRC-6.1-U003" not in " ".join(u for i in issues if "STY-014" in i.message for u in i.unit_ids)  # "may": PRES-004 wins
    assert not any(i.slot_id == "TGT-3-ABBREVIATIONS" for i in issues)  # tables are copied, not checked
    scores = validate(d, slp, drafts, rules).soft_scores
    assert 0 <= scores["gwp_style_compliance"] < 1 and "flesch_reading_ease" in scores and "passive_ratio" in scores

    pd, pdoc, pslp = drafter()  # placement mode: nothing is rewritten, no style rule runs
    placement = pd.build_drafts(await draft_all(pd, None), {})
    assert not [i for i in validate(pd, pslp, placement, rules).issues if i.message.startswith("[GWP]")]


def test_readability_helpers():
    assert (syllables("the"), syllables("record"), syllables("recorded"), syllables("simple")) == (1, 2, 2, 2)
    easy, hard = readability(["The cat sat on the mat."]), readability(["Comprehensive organizational documentation necessitates institutional verification."])
    assert easy[0] > 90 > hard[0] and easy[1] < hard[1]


# ── Risk classifier ───────────────────────────────────────────────────


def test_risk_classifier_tags_high_risk_content():
    assert classify_text("This SOP applies to GxP-relevant systems.") == set()
    assert classify_text("Batches are released in accordance with EU GMP.") == {RiskTag.REGULATORY}
    assert RiskTag.RETENTION in classify_text("Records must be retained for 10 years.")
    assert RiskTag.APPROVAL in classify_text("QA approves the release.")
    assert RiskTag.NUMERIC_LIMIT in classify_text("The temperature must not exceed 25 °C.")
    assert RiskTag.ESCALATION in classify_text("Escalate the deviation to the Head of QA.")
    d, doc, slp = drafter()
    risk = high_risk_units(doc, d.facts)
    assert RiskTag.DEADLINE in risk["SRC-6-U001"]  # "within 5 days"
    assert RiskTag.PROHIBITION in risk["SRC-6.1-U001"]  # "must not be taken"
    assert {RiskTag.APPROVAL, RiskTag.PROHIBITION} <= set(risk["SRC-6-U002"])  # "Never close ... without QA approval"
    assert "SRC-1-U001" not in risk and "SRC-9-U001" not in risk  # plain text; boilerplate is not migrated content


# ── Critic ────────────────────────────────────────────────────────────


async def test_critic_reads_only_reworded_claims_and_stays_advisory():
    d, doc, slp, drafts = await gwp_drafts()
    what = _find(drafts, "TGT-1-WHAT", "SRC-1-U001")

    def respond(prompt, n):
        return CriticOutput(findings=[
            CriticFinding(claim_ids=[what.claim_id], problem="meaning_changed", severity="high", explanation="'explains' vs 'describes'."),
            CriticFinding(claim_ids=["C-NOPE"], problem="omission", severity="high", explanation="not shown"),
            CriticFinding(claim_ids=[what.claim_id], problem="redundancy", severity="high", explanation="twice"),
            CriticFinding(claim_ids=[what.claim_id], problem="source_conflict", severity="high", explanation="conflict"),
        ])

    chain = FakeChain(respond)
    run = await critique(Critic(doc, template(), gwp()), drafts, FakeFactory(CriticOutput=chain))
    prompt = chain.calls[0][1].content
    assert len(chain.calls) == 1 and f"[{what.claim_id}]" in prompt and "source [SRC-1-U001]: This SOP describes" in prompt
    assert "PRES-004" in prompt and "SRC-6-U001" not in prompt  # unchanged claims are not sent
    assert run.reviewed == ["TGT-1"] and len(run.issues) == 3  # the unknown claim ID is dropped
    assert all(i.source == IssueSource.CRITIC and i.gate is None and i.slot_id == "TGT-1-WHAT" for i in run.issues)
    by = {re.match(r"\[critic:(\w+)\]", i.message).group(1): i for i in run.issues}
    assert by["redundancy"].severity == Severity.MEDIUM  # never above medium
    assert repairable(by["meaning_changed"]) and not repairable(by["source_conflict"]) and not repairable(by["redundancy"])

    pd, pdoc, pslp = drafter()
    silent = FakeChain(respond)
    placement = pd.build_drafts(await draft_all(pd, None), {})
    assert (await critique(Critic(pdoc, template()), placement, FakeFactory(CriticOutput=silent))).issues == []
    assert silent.calls == []  # placement mode copies: nothing for the critic


async def test_a_failing_critic_leaves_a_low_note():
    d, doc, slp, drafts = await gwp_drafts()
    chain = FakeChain(lambda prompt, n: RuntimeError("timeout"))
    run = await critique(Critic(doc, template(), gwp()), drafts, FakeFactory(CriticOutput=chain))
    assert len(chain.calls) == 2 and run.failed == ["TGT-1"]
    assert [(i.severity, i.message[:20]) for i in run.issues] == [(Severity.LOW, "[critic:unavailable]")]
    assert not repairable(run.issues[0])


# ── Gates ─────────────────────────────────────────────────────────────


async def test_gates_gaps_resolutions_and_final_status():
    d, doc, slp = drafter()
    drafts = d.build_drafts(await draft_all(d, None), {})
    report = quality_report("J", 1, report_of(validate(d, slp, drafts)), drafts, doc, template(), d.facts)
    gap = next(i for i in report.issues if i.gate == Gate.MISSING_SLOT)
    assert gap.slot_id == "TGT-3-TERMS" and "accept the slot as N/A" in gap.message
    assert report.gate_counts[Gate.MISSING_SLOT] == 1 and report.gate_counts[Gate.NUMERICAL_CHANGE] == 0
    assert report.high_risk_units["SRC-6-U001"] == [RiskTag.DEADLINE]
    assert final_status(report, rendered=True) == JobStatus.HUMAN_REVIEW_REQUIRED

    accepted = report.model_copy(update={"issues": [i.model_copy(update={"resolved": True, "resolution_note": "N/A"})
                                                    if i.issue_id == gap.issue_id else i for i in report.issues]})
    again = quality_report("J", 2, report_of(validate(d, slp, drafts)), drafts, doc, template(), d.facts, accepted)
    assert again.gates_passed and next(i for i in again.issues if i.issue_id == gap.issue_id).resolution_note == "N/A"
    assert final_status(again, rendered=True) == JobStatus.COMPLETED_WITH_WARNINGS  # medium notes remain
    low_only = again.model_copy(update={"issues": [i for i in again.issues if i.severity == Severity.LOW or i.resolved]})
    assert final_status(low_only, rendered=True) == JobStatus.COMPLETED
    assert final_status(low_only, rendered=False) == JobStatus.COMPLETED_WITH_WARNINGS  # no document yet

    no_purpose = [x for x in drafts if x.target_section_id != "TGT-1"]
    missing = quality_report("J", 3, report_of(validate(d, slp, no_purpose)), no_purpose, doc, template(), d.facts)
    assert any(i.gate == Gate.MISSING_SECTION and i.target_section_id == "TGT-1" for i in missing.issues)


async def test_a_passage_in_no_slot_blocks_until_a_reviewer_accepts_leaving_it_out():
    from app.services.migration_v2.quality.gates import awaits_reviewer

    d, doc, slp = drafter()
    drafts = d.build_drafts(await draft_all(d, None), {})
    dropped = slp.model_copy(deep=True)
    purpose = dropped.section("TGT-1")
    purpose.unplaced_unit_ids = ["SRC-1-U002"]
    report = quality_report("J", 1, report_of(validate(d, slp, drafts)), drafts, doc, template(), d.facts, None, dropped)
    issue = next(i for i in report.issues if i.unit_ids == ["SRC-1-U002"] and i.gate == Gate.UNACCOUNTED_SOURCE)
    assert issue.severity == Severity.HIGH and "is in no slot, so it is not in the document" in issue.message
    assert "Both documents build the framework" in issue.message and awaits_reviewer(issue)
    assert final_status(report, rendered=True) == JobStatus.HUMAN_REVIEW_REQUIRED
    assert not any(i.gate == Gate.UNACCOUNTED_SOURCE for i in quality_report(
        "J", 1, report_of(validate(d, slp, drafts)), drafts, doc, template(), d.facts, None, slp).issues)


async def test_issues_on_high_risk_content_block_and_rewordings_are_listed():
    d, doc, slp, drafts = await gwp_drafts(transform=lambda u, t: t.replace("record every", "log every"))
    base = report_of(validate(d, slp, drafts))
    review = ValidationIssue(issue_id="ISS-made-mandatory", severity=Severity.MEDIUM, category=IssueCategory.PRESERVATION,
                             unit_ids=["SRC-6-U001"], message="[PRES-004] made mandatory")
    advisory = ValidationIssue(issue_id="ISS-critic", severity=Severity.MEDIUM, category=IssueCategory.SEMANTIC,
                               source=IssueSource.CRITIC, unit_ids=["SRC-6-U001"], message="[critic:ambiguity] x")
    plain = review.model_copy(update={"issue_id": "ISS-plain", "unit_ids": ["SRC-1-U002"]})
    report = quality_report("J", 1, base.model_copy(update={"issues": base.issues + [review, advisory, plain]}), drafts,
                            doc, template(), d.facts)
    by = {i.issue_id: i for i in report.issues}
    assert (by["ISS-made-mandatory"].gate, by["ISS-made-mandatory"].severity) == (Gate.HIGH_RISK_UNRESOLVED, Severity.HIGH)
    assert by["ISS-made-mandatory"].message.endswith("[high-risk: deadline]")
    assert by["ISS-critic"].gate is None and by["ISS-plain"].gate is None
    reworded = [i for i in report.issues if "reworded claim(s)" in i.message]
    assert len(reworded) == 1 and reworded[0].unit_ids == ["SRC-6-U001"] and reworded[0].gate is None


# ── The stage loop ────────────────────────────────────────────────────

from tests.test_v2_migration_jobs import END_STATUS, Env, _events, _wait_for  # noqa: E402


def gwp_env(tmp_path, critic_respond):
    env = Env(tmp_path)
    guide = env.approved_guide("GWP")
    rules = GwpRuleSet.model_validate_json(Path(guide["rules_path"]).read_text(encoding="utf-8"))
    style = GwpRule(rule_id="STY-042", category=RuleCategory.STYLE, text="Start each step with a verb.",
                    status=RuleStatus.APPROVED)
    Path(guide["rules_path"]).write_text(rules.model_copy(update={"rules": rules.rules + [style]}).model_dump_json(),
                                         encoding="utf-8")
    orch = env.orchestrator()
    critic = FakeChain(critic_respond)

    def rewrite(prompt, n):  # a repair answers with new wording
        suffix = ", as stated." if "REPAIR:" in prompt else ", as described."
        return echo(lambda u, t: t.rstrip(".") + suffix)(prompt, n)

    orch.chain_factory = FakeFactory(RewriteOutput=FakeChain(rewrite), CriticOutput=critic)
    return env, orch, critic


def flag_first(rounds: int, problem: str = "omission"):
    """A fake critic that flags the first claim it is shown, for the first ``rounds`` calls."""
    def respond(prompt, n):
        if n > rounds:
            return CriticOutput()
        claim = re.search(r"^\[(C-[^\]]+)\]", prompt, re.M).group(1)
        return CriticOutput(findings=[CriticFinding(claim_ids=[claim], problem=problem, severity="high",
                                                    explanation="A clause is missing.")])
    return respond


async def test_a_passage_flagged_after_its_repair_ends_in_the_source_wording(tmp_path):
    env, orch, critic = gwp_env(tmp_path, flag_first(rounds=99))
    job = await orch.wait(env.create(orch, gwp_id="GWP").job_id)
    assert job.status == END_STATUS  # only the required 'steps' gap is left for the reviewer
    assert (job.sections[0].status, job.sections[0].attempts) == (SectionStatus.VALIDATED, 2)
    transitions = [e["to_status"] for e in env.store.list_events(job.job_id) if e["to_status"]]
    assert transitions.count("REPAIRING") == 2 and transitions.count("VALIDATING") == 3
    # Round 2 reads only the repaired claim; round 3 has no reworded claim left to read.
    assert len(critic.calls) == 2 and len(re.findall(r"^\[C-", critic.calls[1][1].content, re.M)) == 1
    assert [e["detail"]["last_attempt"] for e in _events(env.store, job.job_id, "repair")] == [[], ["TGT-3"]]
    draft = orch.artifacts.latest(job, ArtifactKind.SECTION_DRAFT, SectionDraft, "TGT-3")
    assert (draft.version, draft.origin) == (3, DraftOrigin.REPAIR)
    first = next(c for c in draft.iter_claims() if c.source_unit_ids == ["SRC-4.2-U001"])
    assert first.text == "The Process Owner creates a deviation record." and not first.rule_ids_applied
    assert any("copied verbatim on the last repair attempt" in n and "omission" in n for n in draft.unresolved_items)
    others = [c for c in draft.iter_claims() if c.source_unit_ids == ["SRC-4.2-U002"]]
    assert others[0].text.endswith(", as described.")  # never flagged: keeps its first rewrite
    report = orch.artifacts.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport)
    assert not any(i.source == IssueSource.CRITIC for i in report.issues)
    assert [e["detail"]["round"] for e in _events(env.store, job.job_id, "validation")] == [1, 2, 3]


async def test_one_repair_clears_a_critic_finding(tmp_path):
    env, orch, critic = gwp_env(tmp_path, flag_first(rounds=1))
    job = await orch.wait(env.create(orch, gwp_id="GWP").job_id)
    assert job.status == END_STATUS
    assert (job.sections[0].status, job.sections[0].attempts) == (SectionStatus.VALIDATED, 1)
    first = next(c for s in orch.artifacts.latest(job, ArtifactKind.SECTION_DRAFT, SectionDraft, "TGT-3").slots
                 for c in s.claims if c.source_unit_ids == ["SRC-4.2-U001"])
    assert first.text.endswith(", as stated.")  # re-worded by the repair, then passed
    report = orch.artifacts.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport)
    # The example's gap, and its reference to a section it does not have (Phase 12).
    assert sorted(i.gate.value for i in report.issues if i.gate and not i.resolved) == ["broken_cross_reference", "missing_slot"]
    gates_event = _events(env.store, job.job_id, "quality_gates")[0]["detail"]
    assert gates_event["status"] == "HUMAN_REVIEW_REQUIRED"
    assert gates_event["gate_counts"] == {"missing_slot": 1, "broken_cross_reference": 1}


async def test_a_source_conflict_goes_to_the_reviewer_without_repair(tmp_path):
    env, orch, critic = gwp_env(tmp_path, flag_first(rounds=99, problem="source_conflict"))
    job = await orch.wait(env.create(orch, gwp_id="GWP").job_id)
    assert job.status == JobStatus.HUMAN_REVIEW_REQUIRED
    assert (job.sections[0].status, job.sections[0].attempts) == (SectionStatus.NEEDS_REVIEW, 0)
    assert not _events(env.store, job.job_id, "repair") and len(critic.calls) == 1
    report = orch.artifacts.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport)
    conflict = next(i for i in report.issues if i.message.startswith("[critic:source_conflict]"))
    assert conflict.severity == Severity.HIGH and conflict.gate is None and not conflict.resolved


# ── API: resolve issues, edit, re-validate ────────────────────────────


@pytest.fixture
def api(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.auth import require_user
    from app.config.settings import get_settings
    from app.main import app

    env = Env(tmp_path)
    app.dependency_overrides[get_settings] = lambda: env.settings
    app.dependency_overrides[require_user] = lambda: {"userId": "reviewer-1"}
    with TestClient(app) as client:
        previous = app.state.migration_orchestrator
        orch = env.orchestrator()
        app.state.migration_orchestrator = orch
        try:
            yield client, orch, env
        finally:
            client.portal.call(orch.stop)
            app.state.migration_orchestrator = previous
            app.dependency_overrides.pop(get_settings, None)
            app.dependency_overrides.pop(require_user, None)


def test_api_resolves_a_gap_and_revalidates_after_an_edit(api):
    client, orch, env = api
    base = "/api/v1/migrations"
    job_id = client.post(base, json={"sop_record_id": env.sop["id"], "template_id": "TPL", "mode": "auto"}).json()["job_id"]
    _wait_for(client, job_id, {"HUMAN_REVIEW_REQUIRED"})
    report = client.get(f"{base}/{job_id}/validation").json()
    gap = next(i for i in report["issues"] if i.get("gate") == "missing_slot")
    assert report["gate_counts"]["missing_slot"] == 1

    assert client.post(f"{base}/{job_id}/issues/ISS-nope/resolve", json={"note": "x"}).status_code == 404
    assert client.post(f"{base}/{job_id}/issues/{gap['issue_id']}/resolve", json={"note": ""}).status_code == 422
    assert not resolve_dangling_reference(client, job_id)["gates_passed"]  # the gap is still open
    done = client.post(f"{base}/{job_id}/issues/{gap['issue_id']}/resolve", json={"note": "N/A for this SOP"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["gates_passed"] and body["status"] == "COMPLETED_WITH_WARNINGS"
    resolved = next(i for i in body["issues"] if i["issue_id"] == gap["issue_id"])
    assert resolved["resolved"] and resolved["resolution_note"] == "reviewer-1: N/A for this SOP"
    assert client.post(f"{base}/{job_id}/issues/{gap['issue_id']}/resolve", json={"note": "again"}).status_code == 409

    # Re-running the gates keeps the reviewer's resolution.
    assert client.post(f"{base}/{job_id}/retry", params={"from_stage": "QUALITY_REVIEW"}).status_code == 200
    _wait_for(client, job_id, {"COMPLETED_WITH_WARNINGS"})
    assert client.get(f"{base}/{job_id}/validation").json()["gate_counts"]["missing_slot"] == 0

    # A reviewer's edit is validated again; repair never overwrites it.
    draft = client.get(f"{base}/{job_id}/sections/TGT-3/draft").json()
    slot = next(s for s in draft["slots"] if s["claims"] and not s["claims"][0].get("is_gap_marker"))
    edited = [{**slot["claims"][0], "text": slot["claims"][0]["text"] + " Within 99 days."}] + slot["claims"][1:]
    assert client.patch(f"{base}/{job_id}/sections/TGT-3/slots/{slot['slot_id']}", json={"claims": edited}).status_code == 200
    assert client.get(f"{base}/{job_id}").json()["sections"][0]["status"] == "drafted"
    assert client.post(f"{base}/{job_id}/retry", params={"from_stage": "VALIDATING"}).status_code == 200
    job = _wait_for(client, job_id, {"HUMAN_REVIEW_REQUIRED"})
    assert job["sections"][0]["status"] == "needs_review"  # the invented number needs the reviewer, not a repair
    report = client.get(f"{base}/{job_id}/validation").json()
    assert report["gate_counts"]["numerical_change"] == 1
    assert client.get(f"{base}/{job_id}/sections/TGT-3/draft").json()["origin"] == "human"


# ── Samples in placement mode ─────────────────────────────────────────


async def test_samples_placement_mode_needs_no_repair(samples):  # noqa: F811
    from app.services.migration_v2.planning.section_planner import SectionPlanner
    from app.services.migration_v2.planning.slot_planner import SlotPlanner
    from app.services.migration_v2.drafting.drafter import Drafter
    from app.services.migration_v2.quality.facts import extract_facts

    tpl, docs = samples
    assert docs
    for doc, golden in docs:
        sections = SectionPlanner(doc, tpl)
        sp = sections.build_plan(sections.propose(), "J", 1)
        slots = SlotPlanner(doc, tpl, sp)
        slp = slots.build_plan(slots.propose(), "J", 1)
        facts = extract_facts(doc, "J")
        d = Drafter(doc, tpl, sp, slp, facts)
        drafts = d.build_drafts(await draft_all(d, None), {})
        validation = validate_drafts(drafts, slp, doc, tpl, facts)
        assert not [i.message for i in validation.issues if repairable(i)], golden["sop_file"]
        report = quality_report("J", 1, report_of(validation), drafts, doc, tpl, facts)
        assert {i.gate for i in report.open_gate_issues} <= {Gate.MISSING_SLOT}, golden["sop_file"]


from tests.test_v2_slot_planner import samples  # noqa: E402,F401  (fixture)

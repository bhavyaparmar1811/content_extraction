"""Live check of the Phase 10 quality loop on the 3 sample SOPs with the app-extracted, approved GWP rules:
GWP rewrite, then validation rounds (deterministic checks and the critic) with targeted repair, then the gates.
After at most two repair rounds no gate may be open except required-slot gaps, which wait for a reviewer.

    SOP_RUN_LLM_TESTS=1 PYTHONIOENCODING=utf-8 pytest -m llm tests/test_v2_quality_live.py -s
"""

import os
from collections import Counter
from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.schemas.v2 import Gate, QualityReport
from app.services.llm.chain_factory import ChainFactory
from app.services.migration_v2.drafting.drafter import Drafter, draft_all
from app.services.migration_v2.gwp.service import load_rule_set
from app.services.migration_v2.planning.section_planner import SectionPlanner
from app.services.migration_v2.planning.slot_planner import SlotPlanner
from app.services.migration_v2.quality.critic import Critic, critique
from app.services.migration_v2.quality.facts import extract_facts
from app.services.migration_v2.quality.gates import final_status, quality_report
from app.services.migration_v2.quality.repair import claim_locations, issue_sections, repair_sections, repairable
from app.services.migration_v2.quality.validator import validate_drafts
from tests.test_v2_slot_planner import samples  # noqa: F401  (fixture)

GWP_RULES = Path(__file__).resolve().parent.parent / "documents" / "GWP" / "BI-VQD-24416-G_v3_rules.json"

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(os.environ.get("SOP_RUN_LLM_TESTS") != "1", reason="set SOP_RUN_LLM_TESTS=1 to call the LLM"),
]


async def test_live_quality_loop(samples):  # noqa: F811
    if not GWP_RULES.exists():
        pytest.skip("app-extracted GWP rules not available")
    rules = load_rule_set(GWP_RULES)
    tpl, docs = samples
    factory = ChainFactory(get_settings())
    for doc, golden in docs:
        sections = SectionPlanner(doc, tpl)
        sp = sections.build_plan(sections.propose(), "J", 1)
        slots = SlotPlanner(doc, tpl, sp, rules)
        slp = slots.build_plan(slots.propose(), "J", 1)
        facts = extract_facts(doc, "J")
        drafter = Drafter(doc, tpl, sp, slp, facts, rules)
        drafts = drafter.build_drafts(await draft_all(drafter, factory), {})
        critic = Critic(doc, tpl, rules)
        fresh, critic_issues, usage, repaired = drafts, {}, Counter(), Counter()
        for attempt in range(3):
            review = await critique(critic, fresh, factory)
            usage.update(review.usage)
            for d in fresh:
                critic_issues[d.target_section_id] = [i for i in review.issues if i.target_section_id == d.target_section_id]
            validation = validate_drafts(drafts, slp, doc, tpl, facts, rules)
            issues = validation.issues + [i for v in critic_issues.values() for i in v]
            located = claim_locations(drafts)
            todo = {s for i in issues if repairable(i) for s in issue_sections(i, located) if repaired[s] < 2}
            if not todo or attempt == 2:
                break
            result = await repair_sections(drafter, drafts, [i for i in issues if repairable(i)], todo,
                                           {s: 2 + repaired[s] for s in todo}, factory,
                                           verbatim_sections={s for s in todo if repaired[s] + 1 >= 2})
            usage.update(result.usage)
            repaired.update(todo)
            drafts = [next((r for r in result.drafts if r.target_section_id == d.target_section_id), d) for d in drafts]
            fresh = result.drafts
        report = quality_report("J", 1, QualityReport(job_id="J", version=1, issues=issues, unit_coverage=validation.unit_coverage,
                                                      soft_scores=validation.soft_scores), drafts, doc, tpl, facts)
        critic_found = sum(len(v) for v in critic_issues.values())
        print(f"\n{golden['sop_file'][:40]}: {final_status(report, True).value}; gates "
              f"{ {g.value: n for g, n in report.gate_counts.items() if n} }; repairs {dict(repaired)}; "
              f"critic findings left {critic_found}; usage {dict(usage)}; scores {report.soft_scores}")
        for i in report.issues:
            if not i.resolved and i.severity.value in ("critical", "high"):
                print(f"   {i.severity.value} {i.gate.value if i.gate else '-'} {i.message[:200]}")
        assert validation.unit_coverage == 1.0, golden["sop_file"]
        assert {i.gate for i in report.open_gate_issues} - {Gate.MISSING_SLOT, Gate.HIGH_RISK_UNRESOLVED} == set(), \
            [i.message for i in report.open_gate_issues]

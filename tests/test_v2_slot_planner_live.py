"""Live check of the slot planner's LLM confirm/correct pass on the 3 sample SOPs (Phase 8).

Runs the rule section plan, then the slot planner with one call per token-bounded block, applies the
corrections, and compares the slot plan with the goldens. With the app-extracted GWP rules when
``documents/GWP/BI-VQD-24416-G_v3_rules.json`` exists (candidates previewed as approved), else in
placement mode. SOP content may go to the Azure OpenAI deployment (user decision, 2026-10-07).

    SOP_RUN_LLM_TESTS=1 pytest -m llm tests/test_v2_slot_planner_live.py -s
"""

import os
from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.services.llm.chain_factory import ChainFactory
from app.services.migration_v2.gwp.service import approve_rules, load_rule_set
from app.services.migration_v2.inspection.golden import callout_comparison, slot_problems
from app.services.migration_v2.planning.section_planner import SectionPlanner
from app.services.migration_v2.planning.slot_planner import SlotPlanCorrections, SlotPlanner, run_confirm
from app.services.migration_v2.planning.slot_validator import validate_slot_plan
from tests.test_v2_slot_planner import samples  # noqa: F401  (fixture)

GWP_RULES = Path(__file__).resolve().parent.parent / "documents" / "GWP" / "BI-VQD-24416-G_v3_rules.json"

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(os.environ.get("SOP_RUN_LLM_TESTS") != "1", reason="set SOP_RUN_LLM_TESTS=1 to call the LLM"),
]


@pytest.mark.parametrize("with_gwp", [False, True])
async def test_llm_confirmed_slot_plans_match_the_goldens(samples, with_gwp):  # noqa: F811
    if with_gwp and not GWP_RULES.exists():
        pytest.skip("app-extracted GWP rules not available")
    rules = approve_rules(load_rule_set(GWP_RULES))[0] if with_gwp else None
    tpl, docs = samples
    factory = ChainFactory(get_settings())
    totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    for doc, golden in docs:
        sections = SectionPlanner(doc, tpl)
        section_plan = sections.build_plan(sections.propose(), "J", 1)
        planner = SlotPlanner(doc, tpl, section_plan, rules)
        proposal = planner.propose()
        chain = factory.create_structured_planner(SlotPlanCorrections, include_raw=True)
        print(f"\n{golden['sop_file'][:40]} ({'GWP' if with_gwp else 'placement mode'})")
        for block in planner.blocks(proposal):
            result = await run_confirm(planner, proposal, block, chain, planner.prompt(proposal, block))
            dropped = planner.apply(proposal, block, result.corrections)
            print(f"   block {block.target_section_ids}: {len(result.corrections.changes)} change(s), "
                  f"{len(result.corrections.callouts)} callout(s), {len(result.corrections.flags)} flag(s) {result.usage}")
            for callout in result.corrections.callouts:
                print(f"      {callout.kind.value} {callout.unit_ids}: {callout.reason}")
            if dropped:
                print(f"      dropped: {dropped}")
            for key in totals:
                totals[key] += result.usage.get(key, 0)
        plan = planner.build_plan(proposal, "J", 1)
        for text, kind, got in callout_comparison(plan, doc, golden):
            print(f"   golden callout {kind:12} → {got:14} {text[:60]!r}")
        assert slot_problems(plan, doc, tpl, golden) == [], golden["sop_file"]
        assert validate_slot_plan(plan, section_plan, doc, tpl).open_gate_issues == []
    print(f"\nTOTAL {totals}")

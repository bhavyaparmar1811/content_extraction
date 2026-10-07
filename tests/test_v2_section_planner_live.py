"""Live check of the section planner's LLM confirm/correct pass on the 3 sample SOPs (Phase 7).

Sends each SOP's compact outline plus a few unit previews to the configured provider (approved by the
user for the samples on 2026-10-06), applies the corrections, and compares the plan with the goldens.

    SOP_RUN_LLM_TESTS=1 pytest -m llm tests/test_v2_section_planner_live.py -s
"""

import os

import pytest

from app.config.settings import get_settings
from app.services.llm.chain_factory import ChainFactory
from app.services.migration_v2.planning.section_planner import SectionPlanCorrections, SectionPlanner, run_confirm
from app.services.migration_v2.planning.section_validator import validate_section_plan
from app.services.migration_v2.inspection.golden import golden_problems
from tests.test_v2_section_planner import samples  # noqa: F401  (fixture)

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(os.environ.get("SOP_RUN_LLM_TESTS") != "1", reason="set SOP_RUN_LLM_TESTS=1 to call the LLM"),
]


async def test_llm_confirmed_plans_match_the_goldens(samples):  # noqa: F811
    tpl, docs = samples
    factory = ChainFactory(get_settings())
    totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    for doc, golden in docs:
        planner = SectionPlanner(doc, tpl)
        proposal = planner.propose()
        prompt = planner.prompt(proposal)
        chain = factory.create_structured_planner(SectionPlanCorrections, include_raw=True)
        result = await run_confirm(planner, proposal, chain, prompt)
        planner.apply(proposal, result.corrections)
        plan = planner.build_plan(proposal, "J", 1)
        print(f"\n{golden['sop_file'][:40]}: {len(result.corrections.changes)} change(s) {result.usage}, prompt {len(prompt)} chars")
        for change in result.corrections.changes:
            print(f"   {change.source_section_ids} → {change.target_key} ({change.mapping_type.value}): {change.reason}")
        for flag in result.corrections.flags:
            print(f"   FLAG {flag.source_section_ids}: {flag.note}")
        for key in totals:
            totals[key] += result.usage.get(key, 0)
        if result.problems:  # invalid changes were dropped after one repair; the plan must still be right
            print(f"   dropped: {result.problems}")
        assert golden_problems(plan, doc, tpl, golden) == [], golden["sop_file"]
        assert validate_section_plan(plan, doc, tpl).open_gate_issues == []
    print(f"\nTOTAL {totals}")

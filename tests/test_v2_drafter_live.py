"""Live check of the drafter on the 3 sample SOPs (Phase 9): placement mode (verbatim excerpts only) and GWP rewrite
with the app-extracted, approved rules. Every planned passage must be in the draft and no gate may open.

    SOP_RUN_LLM_TESTS=1 PYTHONIOENCODING=utf-8 pytest -m llm tests/test_v2_drafter_live.py -s
"""

import os
from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.services.llm.chain_factory import ChainFactory
from app.services.migration_v2.drafting.checks import draft_issues
from app.services.migration_v2.drafting.drafter import Drafter, draft_all
from app.services.migration_v2.gwp.service import load_rule_set
from app.services.migration_v2.inspection.golden import draft_heading_problems
from app.services.migration_v2.planning.section_planner import SectionPlanner
from app.services.migration_v2.planning.slot_planner import SlotPlanner
from app.services.migration_v2.quality.facts import extract_facts
from tests.test_v2_slot_planner import samples  # noqa: F401  (fixture)

GWP_RULES = Path(__file__).resolve().parent.parent / "documents" / "GWP" / "BI-VQD-24416-G_v3_rules.json"

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(os.environ.get("SOP_RUN_LLM_TESTS") != "1", reason="set SOP_RUN_LLM_TESTS=1 to call the LLM"),
]


@pytest.mark.parametrize("with_gwp", [False, True])
async def test_live_drafts_keep_every_passage(samples, with_gwp):  # noqa: F811
    if with_gwp and not GWP_RULES.exists():
        pytest.skip("app-extracted GWP rules not available")
    rules = load_rule_set(GWP_RULES) if with_gwp else None
    tpl, docs = samples
    factory = ChainFactory(get_settings())
    for doc, golden in docs:
        sections = SectionPlanner(doc, tpl)
        sp = sections.build_plan(sections.propose(), "J", 1)
        slots = SlotPlanner(doc, tpl, sp, rules)
        slp = slots.build_plan(slots.propose(), "J", 1)
        facts = extract_facts(doc, "J")
        drafter = Drafter(doc, tpl, sp, slp, facts, rules)
        run = await draft_all(drafter, factory)
        drafts = drafter.build_drafts(run, {})
        issues, coverage = draft_issues(drafts, slp, doc, tpl, facts)
        claims = [c for d in drafts for c in d.iter_claims()]
        print(f"\n{golden['sop_file'][:40]} ({'GWP' if with_gwp else 'placement'}): {run.usage}, {len(claims)} claims, "
              f"{sum(bool(c.rule_ids_applied) for c in claims)} with rules; "
              f"{sum('copied verbatim' in n for d in drafts for n in d.unresolved_items)} slot fallback note(s)")
        assert coverage == 1.0, golden["sop_file"]
        assert [i.message for i in issues if i.gate] == [], golden["sop_file"]
        assert draft_heading_problems(drafts, golden) == [], golden["sop_file"]

"""Prompt for the semantic critic (Phase 10).

The critic reads the claims a GWP rewrite reworded, each next to the source passages it cites, and reports
problems the deterministic checks cannot see: a changed meaning, a dropped clause or condition, an added
statement, a contradiction. It is advisory: its findings never pass or fail a hard gate by themselves; a
high finding sends the slot to targeted repair, and what survives two repairs goes to a reviewer.

The preservation rules are passed in from the job's rule set; this prompt holds no writing rule.
"""

PROMPT_VERSION = "critic/1"

SYSTEM_PROMPT = """You review a migrated procedure document (SOP). Each claim below was reworded from the source passages
printed under it. Compare every claim with its sources and report only real problems.

Problems to report:
- meaning_changed: the claim says something different from its sources (actor, action, object, condition, scope).
- omission: a clause, condition, exception, example list item or qualifier of the sources is missing from the claims
  that cite them.
- condition_lost: an "if", "when", "unless", "only", "before/after" condition or an approval dependency was dropped
  or attached to the wrong action.
- unsupported_addition: the claim adds content the sources do not state (a reason, an actor, a step, a value).
- contradiction: two claims, or a claim and its source, contradict each other.
- source_conflict: the source passages themselves contradict each other (the rewrite cannot fix this).
- wrong_slot: the claim does not answer its slot's question at all.
- ambiguity: the rewording made the claim unclear in a way the source was not.
- redundancy: two claims say the same thing.

Rules:
- {{ref:...}} tokens are cross-references and are correct as they are. Do not report wording or style choices,
  shorter sentences, active voice or a different word order when the meaning is the same.
- Making a plain statement into an instruction ("The QA team checks" -> "QA must check") is reported elsewhere;
  do not report it.
- severity: "high" when a reader would act differently (meaning_changed, omission, condition_lost,
  unsupported_addition, contradiction), "medium" when the meaning holds but a reviewer should look, "low" otherwise.
- claim_ids: the claim IDs exactly as given. explanation: one or two sentences, quoting the words that differ.
- Return no findings when the claims are faithful."""

USER_PROMPT = """PRESERVATION RULES
{rules}

SECTION {section}
{claims}"""

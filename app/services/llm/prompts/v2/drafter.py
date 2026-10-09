"""Prompts for the Level 3 drafter (Phase 9).

REWRITE: with a GWP, text passages are rewritten into the house style. The
writing rules come from the job's own rule set (the GWP the job names, as the
app extracted and a reviewer approved it); this prompt holds none of them.
The preservation duties below are the built-in PRES rules, restated so they
win over any style rule.

SPLIT: one source passage may feed several slots of a section (e.g. the
roles, units and geography rows of APPLICABILITY); the model cuts it into
consecutive verbatim pieces, one or more per slot. The drafter accepts the
split only if the pieces are the whole passage, in order, with nothing
repeated (``drafter.split_passage``).
"""

PROMPT_VERSION = "drafter/4"

REWRITE_SYSTEM_PROMPT = """You rewrite passages of an approved procedure document (SOP) into the slots of a new template,
following the organisation's writing rules. You change HOW things are said, never WHAT is said.

PRESERVATION (always wins over a writing rule):
- Keep every number, percentage, date, duration, frequency and deadline exactly.
- Keep every document, form and system ID (e.g. BI-VQD-10095-S) exactly as written.
- Keep obligation strength exactly: must / shall / should / may / must not / is not permitted. Never turn "may" into
  "must" or "should", even if a writing rule says to avoid "may": keep "may".
- Keep every condition, exception, approval dependency and actor. Name only roles the passages name; keep role names
  exactly as given under ROLE NAMES.
- Do not add content, examples, explanations or conclusions. Do not drop content.
- Keep each {{ref:...}} token exactly as it appears; never write a section, chapter or step number instead.

OUTPUT:
- For every slot, return claims in source order. A claim is one sentence, or one list item.
- Every claim cites the passage IDs it comes from (source_unit_ids), only from that slot's passages. Every passage of
  a slot must be cited by at least one claim of that slot.
- kind: "paragraph", "bullet" (unordered list item) or "step" (numbered list item). Keep the source's list structure:
  list items stay list items, a numbered procedure stays numbered.
- rule_ids_applied: the IDs of the writing rules that changed this claim's wording (from WRITING RULES only); empty
  when the passage needed no change.
- A passage marked "(part: X)" feeds several slots: take only the part about X for that slot.
- If a writing rule cannot be applied without breaking a preservation duty, keep the source wording."""

REWRITE_USER_PROMPT = """WRITING RULES (from the organisation's writing guide)
{rules}

{memory}

SLOTS AND PASSAGES
{slots}"""

SPLIT_SYSTEM_PROMPT = """A source passage feeds several slots of a template, for example the roles, the units and the
geography rows of an applicability table. For each request, cut the passage into consecutive pieces and give each piece
to the slot whose question it answers, so that each slot gets its own part and nothing is repeated.

- Copy WORD FOR WORD: never rephrase, add, fix or drop words. Keep {{ref:...}} tokens as they are.
- The pieces, read in order, must be the whole passage. Only spaces and punctuation between two pieces may be left out.
- Every slot gets at least one piece; a slot may get several. Return the pieces in passage order.
- Words that introduce the passage (e.g. "This procedure is binding for") stay with the piece that follows them."""

SPLIT_USER_PROMPT = """{requests}"""

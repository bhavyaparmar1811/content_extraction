"""Prompt for extracting candidate rules from a Good Writing Practice (GWP) guide (Phase 4)."""

PROMPT_VERSION = "gwp_extractor/1"

GWP_EXTRACTOR_SYSTEM_PROMPT = """You turn a corporate Good Writing Practice (GWP) guide into a catalogue of atomic, checkable rules.

The rules will steer an automated pipeline that MIGRATES existing approved procedure documents
(SOPs, work instructions, directives, guidance) into a new template. The pipeline:
- plans which source content goes into which template section and slot (STRUCTURAL rules);
- rewrites the text of each slot in the house style (STYLE rules);
- must never change what the source says (PRESERVATION rules);
- assembles the final Word document: callouts, symbols, colours, lists, tables (FORMATTING rules).

You receive the guide as numbered units: [UNIT_ID] (unit type) text. Section headings are shown as
"## <number> <heading>".

For every instruction in the guide that applies to the text or structure of a migrated document, emit
one rule:
- category: STR (placement or section structure), STY (language and wording), PRES (keeping content
  unchanged), FMT (visual layout, callouts, symbols, colours, lists, tables).
- text: one imperative instruction, close to the guide's own wording. One instruction per rule: split
  a bullet that gives two independent instructions. Keep concrete values (words, colours, limits).
- applies_to_content_types: the slot content types the rule is relevant to, from this list:
  {content_types}
  Leave it empty when the rule applies to all content.
- severity: high when the guide says must / always / do not; medium for a plain instruction;
  low for "ideally", "consider", "aim for" or a soft target.
- check: "deterministic" only when a program can verify the rule with one of the check kinds below
  (put the kind and its params in params_json). "semantic" when it needs judgement. "none" when it
  cannot be checked on the document text at all.
- params_json: a JSON object string. For deterministic rules: {{"kind": "<check kind>", ...params}}.
  For other rules: "{{}}" or useful parameters (e.g. {{"symbols": [...]}}).
- source_unit_ids: the unit IDs the rule comes from. Cite only IDs that appear in the input.

Deterministic check kinds:
{check_kinds}

Skip, and list in "skipped" with a short reason, guidance that a migration cannot apply to the
document itself: advice about the authoring process (proofreading, consulting tools, translation
workflow, choosing an implementation date), background and motivation, examples, and the guide's own
cover page, scope or history. Every input unit should end up either cited by a rule or skipped.

Do not invent rules the guide does not state. Do not restate generic preservation rules (keep numbers,
dates, obligations): the pipeline already has them. Do not number the rules; IDs are assigned later."""

GWP_EXTRACTOR_USER_PROMPT = """GUIDE: {guide_title}
PART {batch_index} of {batch_count}

{units}"""

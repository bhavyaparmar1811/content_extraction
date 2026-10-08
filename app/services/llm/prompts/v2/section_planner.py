"""Prompt for the Level 1 section planner's confirm/correct pass (Phase 7).

The deterministic planner has already proposed a target for every source
section from its name and its content. The model only checks that proposal
and returns corrections, so input and output stay small.
"""

PROMPT_VERSION = "section_planner/6"

SECTION_PLANNER_SYSTEM_PROMPT = """You review a proposed mapping of a source procedure document (SOP) onto the sections of a new template.

A deterministic matcher proposed a target for every source section from two signals:
- the section NAME (heading, compared with template headings and known synonyms such as SCOPE = APPLICABILITY);
- the section CONTENT (what its paragraphs are: purpose statements, scope statements, definitions, procedure steps,
  restrictions/prohibitions, roles and responsibilities, references, ...).

Check each proposal against the template section's purpose and correct only what is wrong. Typical corrections:
- a subsection whose content belongs elsewhere (e.g. prohibited use cases under SCOPE belong to PROCESS);
- a source section matched to the wrong one of two similar targets (e.g. ASSOCIATED_DOCUMENTS vs REFERENCES);
- content that has no place in the template and should be omitted (give the reason), or that you cannot place (unresolved).

The content profile only DESCRIBES a section (how many of its units look like purpose statements, definitions,
procedure steps, ...). It is not a request: 'other' counts plain text, which is normal in every
section: never move or omit a section because of it.
'associated_document' marks references to this document's own sub-documents (its number plus a suffix such as
-RD00 or -AD02): they belong with associated documents, not with general references.

Rules:
- Use only template keys and source section/unit IDs that appear in the input.
- Keep content in the source order; never invent content.
- Moving a whole subsection: list its section ID. Moving only some units of a section: list the section ID AND unit_ids;
  unit_ids must be ones shown under UNIT PREVIEWS (never make one up).
- If a section you saw no passages of seems to contain some that belong elsewhere, do not change it: add a flag
  (its section ID and one short note for the reviewer) instead.
- A move of a section also moves its subsections unless you list them separately.
- mapping_type: one_to_one, merge, split (part of a source section goes elsewhere), move, omit, unresolved.
- Lines marked [CHECK] are the matcher's doubts and [MOVED] its relocations: decide them, using the unit previews.
- A section whose heading names the template section (e.g. 'PURPOSE' → PURPOSE) stays there as a whole.
- APPLICABILITY takes statements about to whom or where THIS document applies. Procedure content that mentions
  processes, systems, countries or sites as its subject matter (e.g. 'processes in scope of the master list', 'country rollout') stays in PROCESS.
- STRUCTURAL WRITING RULES, when given, describe what each template section should contain. Use them to decide close
  calls; they are never a reason to move a subsection out of the chapter whose heading names its target.
- Return ONLY corrections and flags; do not restate proposals you agree with. If all is right, return empty lists."""

SECTION_PLANNER_USER_PROMPT = """TEMPLATE SECTIONS (key | heading | required | expected content)
{targets}

SOURCE OUTLINE (id | heading | units | content profile | proposed target)
{outline}
{previews}{rules}"""

# Migration v2: Phased Implementation Plan


## Context

The current migrator (`app/services/migration/docx_migrator.py`) works like this:
- One LLM call maps whole source sections and elements into template sections (`section_aligner.py`).
- Source text is copied verbatim into the template.
- Placement relies on fuzzy heading matching against paragraph indices.

It has no stable IDs, no slots or anchors, no GWP rewriting, no traceability and no fact-preservation checks. It also has no persistence for migration jobs, and the run is synchronous.

The target design is `docs/migration_plan.md`:
- Section plan, then slot plan, then GWP drafting, validation, repair, anchor-based rendering, reconciliation and human review.
- Every step must be traceable and versioned.

Decisions made with the user:
- **Templates:** will be normalized with content controls or anchors. The tooling assists, and a readiness report gates migration.
- **GWP:** arrives as a Word or PDF document. It is parsed and classified into rules, and a hand-editable rules JSON is the source of truth after curation. **It is an optional migration input** (decided 2026-10-06): a job uses a GWP only when it names one, and it runs without one. The built-in preservation rules apply either way.
- **Rollout:** the new pipeline runs alongside the old one under `/api/v1/migrations`. `/documents/migrate` keeps working until v2 matches it on golden samples.
- **Frontend:** the review UI is in scope.
- **Sample inputs:** the user is collecting real SOP, GWP and template files. Phase 0 depends on them.

## How to use this plan across Claude Code sessions

- Each phase is sized for about one session and must end green: `pytest` passes and nothing old breaks.
- At the end of each phase:
  - Append to `docs/migration_v2/PROGRESS.md`: what was done, files touched, open issues, and the next phase's first step.
  - Commit with the phase number in the message.
- Start each session by reading `IMPLEMENTATION_PLAN.md`, the relevant phase, and the last `PROGRESS.md` entry. Do not re-explore the whole repo.
- New code lives in `app/services/migration_v2/` (with subpackages) and `app/schemas/v2/`. The old modules are only touched where a phase says so.
- Phases with a ★ can be split into "a" and "b" sessions if they run long.

## How to inspect an SOP (any phase)

`scripts/inspect_sop.py` runs the finished stages on real SOPs, with the production stage code, and writes a self-contained HTML report per SOP plus `index.html`. Each report has checks, the section plan, the passages with facts highlighted, protected facts, the template, text completeness, the golden comparison and the JSON artifacts. Everything stays local; `documents/` is gitignored.

```text
venv/Scripts/python scripts/inspect_sop.py --sop documents/SOPs                 # a folder or one .docx/.pdf
venv/Scripts/python scripts/inspect_sop.py --sop new.pdf --llm                  # also run the LLM confirm call
venv/Scripts/python scripts/inspect_sop.py --sop x.docx --template other.docx --template-config other.json --gwp-rules rules.json
```

Statuses:
- PASS: as expected.
- WARN: look at it (e.g. a mapping to review, an LLM note).
- FAIL: fix before migration (lost text, blocking plan issue, template not ready, golden mismatch).
- INFO: nothing to check.

Each new phase adds its output and checks to the report.

## Phase map

| Phase | Name | Depends on | Session size |
|---|---|---|---|
| 0 | Samples, golden set and quick fixes | sample files | S |
| 1 | v2 data contracts (Pydantic) | 0 | M |
| 2 ★ | SOP extraction v4: source units | 1 | L |
| 3 ★ | Template extraction v3: slots and anchors, plus normalization tool | 1 | L |
| 4 | GWP ingestion and rule catalog | 1 | M |
| 5 | Migration job persistence, orchestrator skeleton, API | 1 | M |
| 6 | Protected-fact registry and deterministic preservation checks | 2 | M |
| 7 | Section planner and section-plan validation | 2, 3, 5 | M |
| 8 | Slot planner and slot-plan validation (done 2026-10-08) | 7 | M |
| 9 ★ | GWP drafter (claim-level evidence, bounded memory, batching) (done 2026-10-08) | 4, 6, 8 | L |
| 10 | Validation, semantic critic, targeted repair, quality gates (done 2026-10-08) | 6, 9 | M |
| 11 ★ | Anchor-based Word renderer v2 (done 2026-10-09) | 3, 9 | L |
| 12 | Document assembly, cross-reference resolution, reconciliation, audit and export (done 2026-10-10) | 10, 11 | M |
| 13 ★ | Frontend: migration review UI | 5, 7, 8, 10 | L |
| 14 | Evaluation harness, calibration and cutover | all | M |

Phases 2, 3 and 4 are independent of each other, as are 6 and 5, and 11 and 10. Phase 13 can start after Phase 8 using API stubs.

---

## Phase 0: Samples, golden set and quick fixes

**Goal:** a real-data baseline and fixes for the bugs that block the new work.
- **Samples:**
  - Create `tests/fixtures/migration_v2/` with `sop/`, `template/` and `gwp/`. Add a README listing each file's provenance and whether it may be committed (confidential files go in a gitignored `data/samples/`).
  - Hand-write at least 2 to 3 "golden" expected outcomes per SOP, even partially: the expected section mapping, a few key facts that must survive, and the expected slot for a few units.
- **Bugs:**
  - Fix `template_store.list_records()` → `get_records()` in `app/api/migration.py:152,185`.
  - Replace the relative `Path("data/template_uploads")` in `migration.py:196` with `settings.template_upload_dir`.
  - Verify the model string format in `settings.py:72` (`gemini/...` vs `provider:model`) with `init_chat_model`.
- **Spike:** run the current extraction on the samples and note the gaps (list nesting, tables, `w:sdt` content skipped) in `PROGRESS.md`.
- **Done when:** the samples are in place, the bugs are fixed, and the existing test suite passes.

## Phase 1: v2 data contracts

**Goal:** freeze the shapes every later phase uses. This phase is code only, with no behavior change.

Create `app/schemas/v2/` containing:
- **`source.py`:** `SourceDocument`, `SourceSection` and `SourceUnit`.
  - `SourceUnit` has `unit_id`, `content_hash`, `section_id`, `seq`, `unit_type`, `text`, `runs?`, `list_level`, `list_number`, `table_ref`, `location{page, paragraph_index, xml_path}`, `relations[]` (for example `warning_for → unit_id`) and `is_boilerplate`.
  - `unit_type` covers paragraph, procedure_step, bullet, warning, note, table_row, table_cell_group, caption, reference, definition and heading_statement.
  - **ID scheme:** `unit_id = f"{section_path}-U{seq:03d}"` for display, plus `content_hash` (normalized text plus structural path) for matching across versions.
- **`template.py`:** `TemplateModel`, `TargetSection` and `TargetSlot`.
  - `TargetSlot` has `slot_id`, `section_id`, `instruction`, `content_type`, `required`, `display_order`, `anchor{kind: content_control|bookmark|table_cell|placeholder|paragraph, ref}`, `instruction_behavior`, `icon?` and `formatting_profile?`.
  - `instruction_behavior` is a single enum: `retain_as_label`, `replace`, `hide_after_population` or `retain_separate`.
  - `content_type` is the enum from §6.3 of `migration_plan.md`.
- **`gwp.py`:** `GwpRule{rule_id, category: STR|STY|PRES|FMT, text, applies_to_content_types[], severity, check: deterministic|semantic|none, params}` and `GwpRuleSet{guide_id, version, rules}`.
- **`plans.py`:** `SectionMapping`, `SectionPlan`, `SlotMapping` and `SlotPlan`, with statuses `mapped`, `source_content_not_found`, `not_applicable`, `unmapped_source_content`, `conflicting_source` and `needs_review`, as in the doc.
- **`draft.py`:**
  - `Claim{claim_id, text, source_unit_ids, spans?, rule_ids_applied}`.
  - `SlotDraft{slot_id, claims[], rendering: paragraphs|ordered_list|bullets|table}` and `SectionDraft`.
- **`quality.py`:** `ProtectedFacts`, `ValidationIssue{issue_id, severity, category, slot_id?, unit_ids, message, gate}` and `QualityReport`.
- **`job.py`:** `MigrationJob`, a `JobStatus` enum (from §22), `SectionState` for per-section status, and `ArtifactRef{kind, version, path, sha256}`.
- **`refs.py`:** `CrossReference{ref_id, from_unit_id, raw_text, ref_kind, target}`.
  - `ref_kind` is one of `section`, `step`, `table`, `figure`, `appendix`, `external_doc`.
  - `target` is a source section or unit ID, or an external document ID.
  - `NumberMap{source_id → target_number}` holds the final numbering, built at assembly time.

Also:
- Add `docs/migration_v2/CONTRACTS.md` with one example JSON per model.
- Write tests for schema round-trips and the enum values.
- **Done when:** the models import cleanly, the examples validate and the tests pass.

## Phase 2 ★: SOP extraction v4 (source units)

**Goal:** produce `SourceDocument` from PDF and DOCX, written next to the existing v3.1 output so nothing breaks.

**Reuse:** `parser/docx_parser.py`, `parser/pdf_parser.py`, `hierarchy/ast_builder.py` and `export/migration_exporter.py` (the header/footer stripping and section detection).

**Changes:**
- **2a, DOCX:**
  - Traverse block-level `w:sdt` (currently skipped at `docx_parser.py:79`).
  - Read `w:numPr`/`w:ilvl`/`numId` to get real list levels and numbering (replacing the style-name-only check at `:277`).
  - Fill in `SourceLocation.paragraph_index`/`xml_path`, which `ast_builder._make_source_loc` (`:642`) currently leaves out.
  - Set `ListNode.nesting_depth` (hard-coded to 0 at `ast_builder.py:514`).
- **2b, export and PDF** (done 2026-10-03, see PROGRESS; PDF path untested, re-upload file naming deferred):
  - New `app/services/export/source_unit_exporter.py`, which maps the AST to `SourceDocument`. It handles:
    - nested sections (no longer flat);
    - table rows as `table_row` units with all cells, including continuation cells;
    - unit typing for warnings and notes (keyword and shading heuristics, a "Note:"/"Warning:" prefix, icon semantics);
    - `relations` linking a warning to the next or previous step;
    - flagging boilerplate (revision history, headers, approval blocks);
    - cell shading as `TableCell.fill_hex`, dropping header fills (header rows, or whole rows in one grey such as `D9D9D9`/`F2F2F2`);
    - **cross-reference detection** (`source_refs.py`). It detects phrases such as "see Section 4.2", "refer to step 5", "as described above", "Table 3", "Appendix B" and "SOP-QA-012". Each one is resolved to a source section or unit ID where possible and stored as a `CrossReference`, with unresolved references flagged.
  - Run the PDF path through the same exporter, with a lower-confidence flag on list levels.
- Wire the exporter into `job_manager.py` after the export stage. Write `data/output/{uid}_v{n}_units.json` and add a `units_path` column to `sop_records` (an additive migration in `sop_store.py`).
- Fix file overwrite on re-upload (`KNOWN_ISSUES` #5): use per-version file names.

**Tests:**
- Nested lists, tables with merged cells, `w:sdt` content, stable `content_hash` across re-runs, reading order, and the samples from Phase 0.

**Done when:**
- Every sample SOP produces units with no lost text. Add a check that the sum of unit text approximately equals the document text.

## Phase 3 ★: Template extraction v3 (slots and anchors) and normalization

**Status:** done 2026-10-03 (see PROGRESS). The optional LLM `content_type` classification is deferred; keyword rules only.

**Goal:** produce a `TemplateModel` with explicit slots and anchors, plus a tool that normalizes templates.

**Reuse:** `extraction/template_extractor.py` (instructions, callouts, icon library), `parser/template_parser.py` (blue detection) and `template_store.py`.

**3a, slot detection** (new `app/services/migration_v2/template/slot_detector.py`). It follows the priority order in §8:
1. `w:sdt` content-control tags (`CC_<section>_<slot>`).
2. Bookmarks.
3. Explicit placeholders (reuse `_PLACEHOLDER_PATTERN`), now with positions.
4. Table instruction and destination cell pairs.
5. Blue instruction paragraphs, with the next non-instruction region as the destination.
6. Icons, as metadata only.

Slot detection also:
- Assigns `content_type` (keyword rules first, then an optional LLM classification cached per template version) and `instruction_behavior`.
- Links each instruction to its slot, replacing the duplicate unlinked instruction copies.
- **Callout palette (done):** `template/callout_palette.py` (`extract_callout_palette`).
  - It reads the legend table and the single-row prototype boxes into `TemplateModel.callout_palette`, matching prototypes to the legend by fill.
  - It ignores shaded rows that sit beside unshaded cells, such as the RACI matrix.
  - Slot detection turns each `CalloutPrototype` into a `TargetSlot` with `formatting_profile=callout` and its `callout_kind`.
  - The normalizer wraps the prototype's text cell in `CC_<section>_CALLOUT_<kind>`.
  - Fill overrides come from `data/template_config/{template_uid}.json` under `callout_palette` (fill hex → kind).
  - A highlighted "(optional)" in a heading sets `TargetSection.optional_marker` and `required=False`.
  - Other highlights, and fills with no kind, go to the readiness report (`PaletteResult.issues`).

**3b, normalization and readiness:**
- `template/normalizer.py` writes a normalized copy of the template. It wraps each detected destination in a tagged `w:sdt` (or adds a bookmark) and keeps the original untouched.
- `template/overrides.py` reads an optional `data/template_config/{template_uid}.json`. Humans use it to fix slot types, behaviors and required flags.
- `template/readiness.py` produces a report: slots without anchors, icons without instructions, ambiguous regions. **A migration is blocked unless the readiness status is `ready`.**
- New endpoints:
  - `POST /templates/{id}/normalize`
  - `GET /templates/{id}/slots`
  - `GET /templates/{id}/readiness`
  - `PUT /templates/{id}/slot-config`
- New `template_records` columns: `model_path`, `normalized_path` and `readiness_status`.

**Tests:**
- Synthetic templates with content controls, table-pair slots, icon-less instructions and merged cells.
- The sample template's readiness report.

**Done when:** the sample template is normalized, every slot has an anchor, and the readiness status is `ready` once a config override is applied.

## Phase 4: GWP ingestion and rule catalog

**Goal:** turn the GWP Word or PDF document into a curated `GwpRuleSet`.

**Steps:**
1. Parse the GWP document with the existing parsers into ordered units (reuse the Phase 2 exporter).
2. An LLM pass (`ChainFactory.create_structured_planner(GwpRuleSet)`) extracts candidate rules, each with a category, `applies_to_content_types`, severity, a check type and parameters. Each candidate cites the source unit IDs it came from.
3. Persist the candidates to `data/gwp/{guide_id}_v{n}_rules.json`. A human edits or approves them, using the API (`GET/PUT /api/v1/gwp/{id}/rules`, `POST /approve`) or the JSON directly.
4. Add `app/stores/gwp_store.py`, a `gwp_guides` table with version and status, using the same pattern as `template_store.py`.
5. Add a rule selector `select_rules(category, content_types)` so later phases send only the rules they need.

Prompts go in `app/services/llm/prompts/v2/gwp_extractor.py`, with a `PROMPT_VERSION` constant. All v2 prompts use this pattern.

**Done when:** the sample GWP produces a rule set that a human has reviewed, and the selector is tested.

## Phase 5: Migration job persistence, orchestrator and API

**Goal:** a durable, asynchronous migration job with the status model. Stage functions are stubbed.

**Store:**
- New `app/stores/migration_store.py` (SQLite, same pattern as the other stores).
- Tables:
  - `migration_jobs`: id, sop_record_id, template_id plus version, gwp_id plus version, status, mode, timestamps, created_by, error.
  - `migration_sections`: job_id, target_section_id, status, attempt.
  - `migration_artifacts`: job_id, kind, version, path, sha256, created_at, created_by.
  - `migration_events`: an audit log.

**Orchestrator:**
- `app/services/migration_v2/orchestrator.py` is a state machine over the §22 statuses, with per-section state.
- It pauses at `SECTION_PLAN_REVIEW_PENDING` and `SLOT_PLAN_REVIEW_PENDING` when `mode=review`.
- It is resumable from persisted state after a restart, which removes the in-memory job problem for v2.
- It runs as an asyncio task. Reuse `LLMRateLimiter` for LLM calls.

**API** (`app/api/migrations_v2.py`, mounted at `/api/v1/migrations`): implement the §25 endpoints, plus `GET /` (list), `POST /{id}/cancel` and `POST /{id}/retry?from_stage=`. They return stub data until later phases fill them in.

**Auth:** require auth on the new router (reuse the `app/api/auth.py` dependencies). This starts addressing `KNOWN_ISSUES` #1 for new code.

Every stage writes an immutable versioned artifact under `data/migrations/{job_id}/{kind}_v{n}.json`.

**Done when:** you can create a job, it moves through the stubbed stages, survives a server restart, and the artifacts are listed.

## Phase 6: Protected facts and deterministic preservation checks

**Goal:** catch meaning changes without depending on an LLM.

New `app/services/migration_v2/quality/facts.py` extracts the following from units, and later from claims:
- numbers, percentages, units and ranges, including written-out numbers ("five" → 5);
- dates, durations, frequencies and deadlines (regex plus a normalizer, for example "5 working days" vs "five business days" as a configurable equivalence);
- modality (`shall`, `must`, `must not`, `may`, `should`, `will`) attached to its clause;
- role names (extracted from the responsibility sections and capitalized noun phrases, then curated);
- document, form and system references (ID patterns).

The output is `ProtectedFacts` for each job, saved as an artifact.

Comparator functions:
- `compare_values(source_units, claims)`
- `compare_modality(...)` (detects weakening such as must → should, or shall → may)
- `compare_roles(...)`
- `compare_references(...)`

Each returns `ValidationIssue`s.

**Tests:** a table-driven suite of allowed and disallowed rewrites, including the examples in §2.1.

**Done when:** the comparators catch every seeded change in the test table with no false positives on the allowed rewrites.

## Phase 7: Section planner

**Goal:** Level 1 mapping from section **names and content**, at minimum token cost (revised with the user, 2026-10-06).

**Rules first, then one compact LLM confirm/correct call per job:**
- **Signals** (`planning/signals.py`, no tokens):
  - **Name:** the heading against target headings, keys and aliases. Built-in defaults, plus `TemplateConfig.sections.<KEY>.aliases`, which land on `TargetSection.aliases`.
  - **Content:** each unit is labelled (purpose, scope, definition, procedure, restriction, responsibility, reference, own-document attachment, record...) using the Phase 6 extractors and table headers. List items inherit their intro line. The labels are matched to each target's slot `content_type`s, with a light bag-of-words cosine against the target instructions.
- **Rule proposal** (`planning/section_planner.py`):
  - Top-level sections are decided name-first.
  - A subsection inherits its parent's target. It moves on content when the margin is clear (`AUTO_SPLIT_MARGIN`), or is flagged [CHECK] when the margin is weaker.
  - Cover page and TOC are omitted. Targets with no source become gaps; optional ones become `not_applicable`.
- **LLM pass:**
  - Input is a compact outline: one line per target and per section (level ≤ 2), with profile and proposal.
  - Unit previews go only with [CHECK] and [MOVED] lines.
  - Output is **corrections only**.
  - Safeguards: one repair retry, and only for invalid changes on doubtful sections; no-op changes ignored; made-up unit IDs dropped; a section whose heading names its target is never moved or omitted wholesale.
  - Token usage is recorded on the plan. On LLM failure the rule plan stands, with its doubts marked `needs_review`.
  - `section_planner_llm: confirm|off`.
- **Validator** (`planning/section_validator.py`): every gate issue blocks approval.
  - references valid;
  - required targets mapped or flagged (`missing_section`);
  - every non-boilerplate unit placed, omitted or flagged (`unaccounted_source`);
  - no unit placed twice;
  - source order kept (`sequence_violation`).
  The report is a `quality_report` artifact with scope `section_plan`.
- **Review:** in `mode=review` the job stops. `PATCH /section-plan` saves a human version, re-validates it and marks changed mappings `origin: human`. `POST /approve` is refused (409) while gate issues are open.

**Done when:** the plans for the sample SOPs validate and match the golden mappings, with the rules alone and with the LLM pass.

## Phase 8: Slot planner

**Goal:** Level 2 mapping, run within each approved section pair.

**Planner** (`planning/slot_planner.py`): one call per target section, or per token-bounded group.
- **Input:** the slots, the mapped units in order, the STR rules and the protected facts in scope.
- **Output:** a `SlotPlan`. One unit may map to several slots. Units that fit no slot go to `supporting_information` or `needs_review`. Slots with no evidence get `source_content_not_found`.

**Callouts:**
- Fixed callout slots are mapped like other slots.
- Promotions go in `SectionSlotPlan.callout_assignments`:
  - A rule pass runs first: `warning` → `attention`, `note` → `explanation`, recorded with `origin=rule`.
  - The LLM may add assignments, but only kinds from the template palette, each with a reason. The prompt gets a closed `kind: label` list and never colours.
  - Reviewers can change or remove assignments with the slot plan.

**Token budgeting** goes in `planning/budget.py`. It estimates tokens per call from the unit text and the slot count, and splits large sections into coherent blocks along sub-heading and step-group boundaries.

**Validator** checks that:
- every mapped unit is placed in at least one slot or flagged;
- the ordered-procedure slot keeps its order;
- required slots are resolved or flagged.

**Review:** endpoints and a pause, as in Phase 7.

**Done when:** the sample section maps to slots consistently with the golden expectations, and large-section splitting is tested.

## Phase 9 ★: GWP drafter

**Goal:** Level 3. Rewrite the approved evidence into slot content, with claim-level traceability.

**Drafter** (`drafting/drafter.py`): one call per target section, or slot group.
- **Input:** slot definitions, the slot plan, the mapped units, STY, PRES and FMT rules selected by content type, protected facts and bounded memory.
- **Output:** a `SectionDraft` of `Claim`s. Each claim must cite its `source_unit_ids`, and claims without citations are rejected by the schema validator.

**Callouts:** a claim gets `callout_kind` from its fixed callout slot, or from the `callout_assignments` covering its source units. The drafter never chooses a kind itself.

**Summary slots:** the responsibility, timing and restriction slots are derived from the claims of the primary procedure slot (claim reuse or a derived flag). This prevents contradictions between them.

**Bounded memory** (`drafting/memory.py`): approved terms, roles, established facts with their source, and adjacent-section one-line summaries, under a configured token cap.

**Batching:** split by token and complexity using `budget.py` from Phase 8. Sections can be drafted in parallel under `LLMRateLimiter`.

**Cross-references:** the drafter never writes a literal section or step number.
- It keeps every reference as a token, for example `{{ref:SRC-4.2}}` or `{{ref:SRC-4.2-U005}}`, carried inside the claim.
- The memory includes the approved section map, so the drafter knows which target section a referenced source section went to.
- Each reference's raw text and resolved target are passed in with the units. A test checks that every source reference survives as a token.

**Missing evidence:** for a `source_content_not_found` slot, emit a gap marker draft and never generate content.

**Without a GWP (decided 2026-10-06):** a job with no `gwp_id` runs in **placement mode**. The drafter puts the SOP content into the template slots largely as written, and applies only what the template defines:
- section and slot structure;
- numbering (Word lists and headings, with cross-references as `{{ref:...}}` tokens);
- callouts (fixed callout slots and the slot plan's `callout_assignments`).
It does no style rewriting: no voice, wording or sentence-length changes, and no STY rules. The changes it may make are only those needed to fit a slot: splitting a paragraph across slots, turning a numbered source list into the slot's list, and dropping a heading that the template already provides. Each claim still cites its source units (`MigrationAction.COPY_VERBATIM` by default). The built-in PRES rules and the Phase 6 and Phase 10 checks run exactly as with a GWP. With a GWP, the drafter rewrites to its STY, STR and FMT rules as described above.

**Done when:**
- every claim in the sample draft has citations;
- the Phase 6 comparators pass on the sample, or flag real issues;
- the draft is persisted as a versioned artifact.

## Phase 10: Validation, critic, repair and gates

**Goal:** the quality loop.

**Deterministic validator** (`quality/validator.py`):
- structure, meaning that slots are resolved and anchors exist;
- traceability, meaning every unit is accounted for and every claim is cited;
- preservation, using the Phase 6 comparators;
- order, meaning the procedure claims follow the source sequence;
- formatting, meaning no leftover `[TBD]` markers and behaviors applied;
- callouts, meaning every `Claim.callout_kind` matches its slot's kind or an assignment covering its units.

**Semantic critic** (`quality/critic.py`): one LLM call per section that returns `ValidationIssue`s. It is advisory and can never pass a hard gate by itself.

**Repair** (`quality/repair.py`):
- re-draft only the affected slot, giving it the issue list;
- maximum 2 attempts, then `HUMAN_REVIEW_REQUIRED`;
- follow the failure-handling table in §18.

**Gates** (`quality/gates.py`):
- implement the hard gates from §21;
- tag high-risk content with a deterministic risk classifier for acceptance criteria, safety, deadlines, numeric limits and prohibitions;
- count unresolved required-slot gaps; export is blocked until each one is filled or accepted as N/A;
- produce a `QualityReport` artifact.

**Done when:** seeded faults (dropped number, weakened modality, reordered step) are caught and repaired, or escalated.

**As built (2026-10-08):**
- The critic reads only claims a GWP rewrite reworded (placement mode costs nothing), and after a repair only the claims whose text changed; its findings on unchanged claims carry over. Re-reading a whole section let gpt-4o flag different claims each round.
- A section's last repair attempt copies the flagged passages from the source instead of asking the LLM again (meaning before style, §2.1); a note lists them. Gaps, source conflicts, wrong-slot findings and reviewer-edited sections are never repaired.
- High-risk content (§21) comes from a deterministic classifier (`quality/risk.py`); an open medium-or-worse finding on it blocks completion until a reviewer resolves it. Resolving a required-slot gap accepts it as N/A.

## Phase 11 ★: Anchor-based Word renderer v2

**Goal:** Level 4. Insert content at exact anchors in a copy of the normalized template.

**Renderer** (`render/renderer.py`):
- Locate anchors (`w:sdt` tag, bookmark or table cell) and apply `instruction_behavior`.
- Insert the claims as paragraphs, lists or tables.
- Remove or keep instructions as configured (reuse the `instruction_cleaner.py` blue detection, targeted at slot instructions only).

**Reuse and fixes in rendering:**
- Reuse the run and paragraph formatting from `docx_styler.py`, with two fixes:
  - insert in place at the anchor instead of `doc.add_*` followed by a move;
  - use real Word numbering (`w:numPr` with the template's own numbering definitions) instead of literal "1.\t" text.
- **Callouts** (`render/callouts.py`): don't reuse `callout_builder.py`, whose colours and 1×1 layout don't match the template.
  - A fixed callout slot is written into the prototype box's text cell, keeping its icon cell and fills.
  - Claims with a `callout_kind` are grouped by adjacency. Each group goes into a deep copy of the palette prototype table (`CalloutStyle.prototype_table_index`) for that kind.
  - With no prototype, build a 2-column icon and text table from the `CalloutStyle` (fill on both cells, `column_widths_pt`, no borders).
  - Write `tcPr` children in schema order with a single `w:shd`.
  - Source table cells get `w:shd` from `TableCell.fill_hex`.
  - The legend table is removed as an instruction region.
  - A section with `optional_marker` loses its highlighted "(optional)" text when populated, and is removed when empty.
- Tables reuse `table_migrator.py`, populating the template's own tables when the anchor is a table.
- The TOC reuses `toc_builder.py`.
- **Empty slots (decided 2026-10-07):**
  - An optional slot with no content (`not_applicable`) is removed, together with its instruction text. Unused conditional blocks and example rows are removed too.
  - A required slot with no content (`source_content_not_found`) renders in the **review draft only** as a visible, styled "Source content not found. Human review required." marker that the gates count. The reviewer either accepts it as N/A or supplies content.
  - The **final export** removes accepted gap slots and their instruction text.
  - No content is ever generated to fill a gap.

**Tests:**
- open the output with python-docx and confirm content sits under the right anchors;
- check the numbering XML;
- confirm icons, headers and footers are kept;
- confirm a gap renders.

**Done when:** the sample migration produces a `.docx` that a human judges layout-correct.

**As built (2026-10-09):**
- `render/` (`renderer.py`, `content.py`, `tables.py`, `callouts.py`, `numbering.py`, `instructions.py`, `verify.py`). ASSEMBLING renders the review draft (`docx` and `render_report` artifacts); Phase 12 adds the number map before it. `docx_styler.py`, `table_migrator.py` and `toc_builder.py` were not reused: their builders work on v1 schemas and append-then-move; the renderer writes WordprocessingML at the anchor.
- Blue instruction text is removed with the same colour classifier as slot detection (`template/colour.py`, moved out of `slot_detector.py`). Headings are never removed, whatever their style colour.
- Lists: bullets reuse the template's bullet definition; numbered lists get a decimal definition derived from its indents (the GP template has none) and one `w:num` per list that restarts at 1. Sub-headings use `Heading n` with the template headings' numbering (6.1, 6.2.1).
- A source table fills the template table by position when it has no more columns than the template (a column whose headers share no word is a warning); otherwise, or when the template header has placeholders ("[Role 1]"), it is written in its own columns with the template table's formatting, widths from its content, and rows that may break across pages. Each further source table in a slot gets its own table. No cell is dropped.
- Cells keep their paragraphs: `TableCell.paragraphs` (Phase 2 exporter) lists them when there are several.
- Conditional regions: an inline choice without a source line of its own takes the document type another region settled; with none it is removed (warning). A block region is kept only when the slot plan has a `RegionChoice` for it.
- Word refreshes the table of contents on opening (`w:updateFields`). Drawing IDs are renumbered. A replaced bookmark paragraph's bookmark moves to the new content.
- The post-render check (`verify.py`) writes `RenderReport.problems`; a job whose document has a problem cannot end COMPLETED.

## Phase 12: Document assembly, reconciliation, audit and export

**Order of operations:**
1. Draft all sections.
2. **Assemble the whole-document draft model** (all `SectionDraft`s in target order).
3. Build the `NumberMap`: final target section, step, table and figure numbers.
4. Resolve reference tokens.
5. Reconcile.
6. Validate again.
7. **Then** render to Word (Phase 11 renderer).
8. Run a final post-render check.

Rendering happens only after the document is complete and consistent as data.

**Cross-reference resolution** (`assembly/xref_resolver.py`):
- Resolve each token to its new number. Wherever possible, render it as a Word `REF` field pointing to a bookmark on the target heading or step, so the number stays correct if the document is edited later.
- **Reference to merged content:** it points to the merged target.
- **Reference to split content:** it points to the target that holds the referenced unit.
- **Reference to omitted or unmapped content, or an unresolvable reference:** raise a `ValidationIssue` (a high-risk gate).
- **External document references:** kept verbatim as protected values.
- Relative phrases such as "above" or "below" are checked to confirm the target still comes before or after the reference.

**Reconciliation** (`quality/reconcile.py`):
- **Deterministic checks:** terminology, abbreviation first-use, role names, numbering and cross-reference resolution.
- **Narrow LLM pass:** finds contradictions or duplicates across sections. It proposes patches, and each patch is a claim edit with a reason. A patch is applied only if it passes validation again.

**Audit:**
- Every LLM call logs the prompt version, model, token usage, latency, retry count and supplied unit IDs to `migration_events`.
- Human edits store the diff and reviewer identity.

**Export:**
- `GET /api/v1/migrations/{id}/export/word` (the `/api/v1/export/word/{id}` path in `migration_plan.md` §25 moves here to keep one API family), plus `/traceability`, a claim → unit → source location mapping in JSON or CSV.
- Exporting the Word file marks the `sop_records` version with the migrated artifact.
- The final export removes reviewer-accepted gap slots and their instructions (see Phase 11, "Empty slots"). Export is refused while any gap is still unresolved, and each removed gap is recorded in the audit trail.

**Done when:** a full job runs end to end in auto mode on the sample, and the audit trail is complete.

## Phase 13 ★: Frontend migration review UI

**Base:** React, TanStack Query and the existing `ReviewPage`/`MigrationReviewMode`. Types are added to `frontend/src/types.ts`, ideally generated from OpenAPI with `openapi-typescript` instead of copied by hand.

- **13a:**
  - Start a migration from `SopTable` (choose template and GWP; block on template readiness).
  - Job status page with per-section progress.
  - Section-plan review: a two-column source vs target mapping, drag to remap, a justification field for omissions, and approve.
- **13b:**
  - Slot-plan review per section: units on the left, slots on the right, multi-assign.
  - Draft review: slot editor using Tiptap (already installed). Hovering a claim highlights its source units, and edits create a new version.
  - Issue panel fed by the `QualityReport`, with high-risk items first.
  - Compare and version view, mounting the existing `CompareView` and `VersionHistory`.
  - Export button.
- Retire the mock fallbacks for the migration mode in `lib/api.ts`.

**Done when:** a reviewer can take the sample SOP from start to export entirely in the UI.

## Phase 14: Evaluation, calibration and cutover

- `scripts/eval_migration.py` runs v2 on the golden set and scores:
  - section-mapping accuracy;
  - slot-mapping precision and recall;
  - preservation-issue counts;
  - unit coverage;
  - gate pass rate.
- Run the old migrator on the same inputs for a baseline.
- Calibrate the confidence and soft-score thresholds. Record the prompt and model versions per run.
- **Cutover criteria:** v2 is at least as good as v1 on coverage, has zero hard-gate failures on approved goldens, and the reviewer sign-off is complete. Then point the UI at v2 and mark `/documents/migrate` deprecated.

---

## Cross-cutting rules for every phase

- No content is ever generated without `source_unit_ids`.
- Instruction behavior comes from template metadata or config and is never decided by the LLM.
- Every LLM call uses a versioned prompt module, structured output through `ChainFactory`, and `LLMRateLimiter`.
- Every artifact is immutable and versioned. Edits create new versions.
- Tests that need the real LLM are marked `@pytest.mark.llm` and skipped by default. All other tests mock the model.
- Leave a hook for translation in later releases: `SourceDocument.language`, and claims as the unit of translation. Nothing for translation is built in these phases.

## Critical existing files

- **Reused:**
  - `app/services/parser/docx_parser.py`, `parser/pdf_parser.py`, `parser/template_parser.py`
  - `app/services/hierarchy/ast_builder.py`, `app/schemas/ast_nodes.py`
  - `app/services/export/migration_exporter.py`
  - `app/services/extraction/template_extractor.py`
  - `app/services/llm/chain_factory.py`, `rate_limiter.py`
  - `app/services/migration/docx_styler.py`, `table_migrator.py`, `callout_builder.py`, `toc_builder.py`, `instruction_cleaner.py`
  - `app/stores/*_store.py`
- **Modified:**
  - `app/services/job_manager.py` (writes the units)
  - `app/stores/sop_store.py`, `template_store.py` (additive columns)
  - `app/main.py` (mounts the routers and stores)
  - `app/api/migration.py` (Phase 0 bug fixes)
  - frontend `pages/ReviewPage.tsx`, `components/review/*`, `lib/api.ts`, `types.ts`

## Verification

- **Each phase:** `pytest` (all tests green). Phase-specific tests are listed in each phase's "Done when" criteria.
- **LLM phases:** `pytest -m llm` on the samples, with a manual review of the output artifacts in `data/migrations/{job_id}/`.
- **End to end (from Phase 12):**
  1. Upload the sample SOP, template and GWP.
  2. Normalize the template and confirm it is ready.
  3. Run `POST /api/v1/migrations` in review mode.
  4. Approve the plans.
  5. Inspect the `QualityReport`.
  6. Export the `.docx` and open it in Word.
- **From Phase 14:** `python scripts/eval_migration.py` scores are compared against the v1 baseline.

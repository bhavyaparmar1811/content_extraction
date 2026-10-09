# Migration v2: Data Contracts

The Pydantic models in `app/schemas/v2/` are the shapes every v2 phase reads and writes. Example documents live in [`examples/`](examples/). They are validated by `tests/test_v2_contracts.py`, so they always match the code.

All models extend `V2Model`, which **rejects unknown fields**. `to_clean_dict()` returns JSON-ready output without `None` values.

## How the contracts fit together

```text
SourceDocument ─┐                         ┌─ GwpRuleSet (selected per call)
 (sections,     │                         │
  units, refs)  ├─► SectionPlan ─► SlotPlan ─► SectionDraft (claims) ─► assembled draft
TemplateModel ──┘    (Level 1)    (Level 2)      (Level 3)                  │
 (sections, slots,                                                          ├─► NumberMap → resolve {{ref:…}}
  anchors)                                                                  ├─► QualityReport (issues, gates)
                                                                            └─► Word render (Level 4)
MigrationJob tracks status, per-section state and every versioned ArtifactRef.
```

## Models

| Module | Models | Example |
|---|---|---|
| `source.py` | `SourceDocument`, `SourceSection`, `SourceUnit`, `TableRef`, `UnitRelation`, `SourceAsset`, `compute_content_hash()` | [`source_document.json`](examples/source_document.json) |
| `template.py` | `TemplateModel` (`slot_by_ref`), `TargetSection` (`key`), `TargetSlot` (`key`), `SlotAnchor`, `InstructionBehavior`, `CalloutStyle` | [`template_model.json`](examples/template_model.json) |
| `gwp.py` | `GwpRuleSet`, `GwpRule`, `RuleCategory` (`STR`, `STY`, `PRES`, `FMT`), `RuleOrigin`, `GwpExtractionReport` | [`gwp_rules.json`](examples/gwp_rules.json), [`gwp_report.json`](examples/gwp_report.json) |
| `plans.py` | `SectionPlan`, `SectionMapping`, `SlotPlan`, `SectionSlotPlan`, `SlotMapping`, `CalloutAssignment` | [`section_plan.json`](examples/section_plan.json), [`slot_plan.json`](examples/slot_plan.json) |
| `draft.py` | `SectionDraft`, `SlotDraft`, `Claim`, `EvidenceSpan` | [`section_draft.json`](examples/section_draft.json) |
| `quality.py` | `ProtectedFacts`, `ValidationIssue`, `QualityReport`, `Gate`, `RiskTag`, `Modality` | [`quality_report.json`](examples/quality_report.json) |
| `render.py` | `RenderReport`, `SlotRender`, `RegionRender`, `RenderMode` (`review`, `final`), `SlotOutcome`, `RegionOutcome` | [`render_report.json`](examples/render_report.json) |
| `job.py` | `MigrationJob`, `JobStatus`, `SectionState`, `ArtifactRef`, `ArtifactKind` | [`migration_job.json`](examples/migration_job.json) |
| `refs.py` | `CrossReference`, `NumberMap`, `NumberMapEntry`, `NumberKind`, `AssembledDocument`, `ResolvedRef`, `RefStatus`, `make_ref_token()`, `find_ref_tokens()` | [`number_map.json`](examples/number_map.json), [`assembled_draft.json`](examples/assembled_draft.json) |
| `trace.py` | `Traceability`, `TraceRow` | [`traceability.json`](examples/traceability.json) |
| `common.py` | `V2Model`, `ContentType`, `Severity`, `CalloutKind`, `HexColor` | n/a |

## Rules enforced by the models

| Rule | Where |
|---|---|
| Unit and section IDs are unique within a document | `SourceDocument` |
| A `table_row` unit carries its `table_ref` (all cells and headers) | `SourceUnit` |
| Only a `figure` unit carries a figure asset, and every figure unit has one; icons are `icon` assets on the unit they sit beside | `SourceUnit` |
| A template can only be `ready` when every slot has a non-proximity anchor | `TemplateModel` |
| A slot is listed under the section it names | `TemplateModel` |
| Section keys are unique in a template, slot keys unique in a section (so `SECTION.slot` refs are unambiguous) | `TemplateModel` |
| A `one_of` group names two or more existing slot keys of its section | `TargetSection` |
| A template can only be `ready` when every conditional region is in a content control | `TemplateModel` |
| A `table_cell` anchor has table, row and column indices | `SlotAnchor` |
| A rule ID starts with its category (`PRES-004` is a PRES rule) | `GwpRule` |
| Rule IDs are unique in a rule set | `GwpRuleSet` |
| On save (not in the model): a `guide` rule cites existing guide units; only built-in rules are `baseline`; a `deterministic` rule's `params` name a known check kind with its params | `gwp.service.validate_rule_set` |
| A `split` mapping lists the units that go to its target | `SectionMapping` |
| An `omit` mapping has a justification | `SectionMapping` |
| A `mapped` slot cites source units; a `source_content_not_found` slot cites none and is flagged for review | `SlotMapping` |
| **Every claim cites `source_unit_ids`**, except gap markers | `Claim` |
| Completion is blocked while any issue with a `gate` is unresolved | `QualityReport.gates_passed` |
| Colours are 6-digit hex, upper-case, no `#` (the `w:shd/@w:fill` form) | `HexColor` |
| Each callout kind appears once in the palette | `TemplateModel` |
| A slot has `formatting_profile: callout` exactly when it has a `callout_kind`, and that kind is in the palette | `TemplateModel` |
| A template can only be `ready` when every callout kind used by a slot has a prototype box to clone | `TemplateModel` |
| A source unit is promoted into at most one callout; every promotion has a reason | `SectionSlotPlan`, `CalloutAssignment` |

## Conventions

- **IDs.**
  - Source sections and units: `SRC-4.2`, `SRC-4.2-U003` (display IDs). `content_hash` matches units across re-uploads when display IDs shift.
  - Target sections and slots: `TGT-3`, `TGT-3-STEPS`.
  - Template refs: `<SECTION_KEY>.<slot_key>`, for example `APPLICABILITY.geography` (`TemplateModel.slot_by_ref`). Goldens and the template config use them. The slot ID is `TGT-<number>-<KEY>`.
  - Template content controls are tagged `CC_<SECTION_KEY>_<SLOT_KEY>`, for example `CC_APPLICABILITY_GEOGRAPHY`. The normalizer writes them, and the renderer finds slots by them. Each example row of a table slot has its own row-level control with the slot's tag (the first is the row to clone), because Word splits a multi-row control into a separate table on save.
  - Conditional regions are tagged `COND_<SECTION_KEY>_<n>`.
  - Claims: `C-TGT-3-001`.
  - Rules: `STY-002`.
  - Jobs: `MIG-<yyyymmdd>-<8 hex>`, for example `MIG-20261006-3FA2C91B`.
- **Cross-references.** The drafter never writes literal section or step numbers. It writes `{{ref:<source_id>}}` (from `make_ref_token`). After the whole document is assembled, the `NumberMap` resolves each token to its final number and Word bookmark.
- **Instruction behavior** is one of `retain_as_label`, `replace`, `hide_after_population`, `retain_separate`. It comes from template metadata or config, never from the LLM.
- **Summary slots** (responsibility, timing, restriction) reuse procedure claims through `derived_from_claim_ids`, so they can't contradict the procedure.
- **Versioning.** Plans, drafts and reports carry a `version`. Every saved version is an `ArtifactRef` on the job. Human edits create a new version with `origin: "human"`.
- **Job artifacts** are immutable files `data/migrations/{job_id}/{kind}[_{scope}]_v{n}.json` (`docx_v{n}.docx` for the rendered document), never overwritten. `ArtifactRef.sha256` is checked on every read. A plan's `version` equals its artifact version. Approval writes a new version with `approved_by` set. PARSING snapshots the source model, template model and GWP rules, so later edits to the SOP, template or guide never change a running job.
- **Callouts.** The template's colour-coded boxes form `TemplateModel.callout_palette`, one `CalloutStyle` per `CalloutKind` (`introduction`, `explanation`, `attention`, `key_takeaway`) with the template's exact fill, icon and layout.
  - Content gets a kind in two ways. A **fixed callout slot** (`TargetSlot.callout_kind`) is a box placed in the template. A **promotion** (`SectionSlotPlan.callout_assignments`) wraps source units, by a rule (warning → attention, note → explanation), the LLM or a human, and is reviewed with the slot plan.
  - Claims carry the kind as `Claim.callout_kind`. The renderer clones the palette's prototype table, so colours never pass through the LLM.
  - Meaningful source cell colours are kept as `TableCell.fill_hex`; plain header fills are dropped.
  - A cell with several paragraphs lists them in `TableCell.paragraphs` (`text` joins them with spaces, as before), so the renderer keeps the cell's lines.
- **Icons and figures.** A source icon is metadata, a `SourceAsset(kind="icon")` on the unit beside it, and never content. Template slots are matched by position and instruction, not by icon image, because source and template icon files differ. A picture displayed larger than 96 px is a `figure` unit, with its caption as a separate `caption` unit linked by `caption_of`.
- **Template config** (`data/template_config/{template_uid}.json`, not a contract model but validated by `TemplateConfig`): `callout_palette` (fill → kind), `regions` (ambiguous region ID → `fixed` / `instruction` / `slot` / `conditional`), `sections` (key → `required`, `one_of`), `slots` (ref → `content_type`, `required`, `instruction_behavior`, `formatting_profile`). Region IDs are `p:<n>` (n-th body paragraph) and `t:<n>` (n-th body table), counted by `iter_body_blocks`; normalization keeps them.
- **Conditional regions** (`TargetSection.conditional_regions`): template content whose fate depends on the SOP, settled per migration. An `inline_choice` is black text with a blue choice ("This Directive/SOP/...:" → "This SOP:"). A `block` is kept or removed (for example the competence table, kept only when the SOP assigns competences).
- **One-of groups** (`TargetSection.one_of`): slot keys of which at least one must be filled, for example `[["roles", "raci"]]` for "table A, B, or both". Each slot in a group is optional on its own.
- **GWP rules.** `origin` says where a rule comes from: `guide` (extracted, cites `source_unit_ids` in the guide's own `SourceDocument`), `baseline` (built-in `PRES-001`..`PRES-006`, always approved, re-added if deleted) or `manual` (added by a reviewer). Only `approved` rules are selected. A `deterministic` rule has `params.kind` from `gwp/checks.py` (`forbidden_terms`, `max_sentence_words`, `passive_ratio`, `readability`, `callout_palette`, `modality`, `protected_values`), plus an optional `note`. Preservation outranks style: a style rule never changes obligation strength (`PRES-004`).
- **Protected facts** (`ProtectedFacts`, built by `quality/facts.py` in PARSING, artifact `protected_facts`). `ProtectedValue.normalized` is the comparison form:
  - durations and deadlines: `<=5 business_day` ("within five working days");
  - frequencies: `1/1 year` ("annually", "every 12 months");
  - percentages and numbers: `>=95%`, `2 °c`;
  - dates: ISO `2025-09-17`;
  - references: upper-cased (`BI-VQD-10095-S`).
  The registry decides what must be kept, and a reviewer may delete a false fact. A claim may repeat anything its cited units say. Obligations are per sentence; `FUTURE` ("will") is not stored.
- **Section plans.** `SectionMapping.origin` records who decided a mapping: `rule` (the deterministic name and content matcher), `llm` (a correction) or `human` (a reviewer edit). `SectionPlan.origin` is `rule` when no LLM ran. `prompt_version`, `model` and `token_usage` record the LLM pass. In a `split` mapping, a listed section with none of its units in `unit_ids` counts as whole. A source chapter whose subsections go to different targets gives split mappings on both sides. `TargetSection.aliases` holds other source headings for a target (from `TemplateConfig.sections.<KEY>.aliases`). The plan's validation report is a `quality_report` artifact with scope `section_plan`.
- **Slot plans.** One `SectionSlotPlan` per template section, with a `SlotMapping` for every slot.
  - `SlotMapping.source_unit_ids` are in source order. One unit may feed several slots; each of those slots then has `extraction_scope: [<slot key>]` ("only the part about the geography").
  - `SlotMapping.below_unit_ids` (a subset of `source_unit_ids`): in a section whose slots are all tables, the narrative and figures (a run of passages with a figure, a caption or long text) go below the section's tables, in its last table slot; the drafts list them after the rows, and the renderer writes them after the table. Set by rule; the LLM cannot move them.
  - A passage in no slot (`SectionSlotPlan.unplaced_unit_ids`) is not in the document. The job's quality report gives it a high `unaccounted_source` issue (message starts with `[unplaced]`): the job needs a reviewer, who places it or resolves the issue to accept leaving it out. The slot planner's LLM can never take a passage the rules placed out of every slot.
  - `migration_action` is `copy_verbatim` in placement mode (no GWP style rule for the slot's content type) and for table slots; otherwise `extract_and_rewrite`. Empty slots are `none`.
  - `rule_ids` are the approved GWP rules (STY, PRES, FMT) the drafter applies to the slot, selected by content type from the job's `gwp_rules` artifact. Without a GWP they are the baseline `PRES-*` rules. `SlotPlan.gwp_guide_id` names the rule set (`BASELINE` without a guide).
  - Empty slots: required → `source_content_not_found` (gap for the reviewer), optional → `not_applicable` (removed). A `one_of` group with one member filled marks the others `not_applicable`; with none filled, its first member is the gap.
  - A fixed callout slot lists the units promoted to its kind in that section (`callout_assignments`).
  - `unplaced_unit_ids` fit no slot and go to the reviewer, with the reason in `notes`. `RegionChoice` records a source lead-in that answers an inline-choice region ("This SOP is applicable:" → `p:32`, choice `SOP`).
  - `origin` per slot mapping and `prompt_version`, `model`, `token_usage` on the plan, as for section plans. The validation report is a `quality_report` artifact with scope `slot_plan`.
- **Drafts.** One `SectionDraft` per template section (artifact `section_draft`, scope = target section ID), with a `SlotDraft` per drafted slot; `not_applicable` slots are not drafted.
  - `Claim.spans` (`EvidenceSpan`: `unit_id`, `start`, `end`): set when a passage fed several slots and was split between them. They index the passage text with its reference tokens, whitespace collapsed (`drafter._norm(tokenize(unit))`), and mark the part this slot drafted. The draft checks and the critic compare such a claim with its part only. Empty otherwise: the claim stands for its whole passages.
  - `Claim.kind`: `paragraph`, `bullet`, `step` (numbered), `table_row` (cells from the cited unit's `table_ref`), `figure` (image from the cited unit), `caption`, `heading`. Tables, figures, captions and headings are always copied, never rewritten.
  - A `heading` claim is a source sub-heading kept inside a template section (the template provides the section heading itself). It cites `source_section_id` instead of units; `list_level` is its depth below the template heading (0 = first level). Headings are kept only in a section's single free-text slot.
  - A `source_content_not_found` slot holds one gap marker (`is_gap_marker`, text "Source content not found"); nothing is generated for it.
  - `callout_kind` comes from the slot plan's `callout_assignments` (or the fixed callout slot); promoted passages are drafted once, in their content slot.
  - `rule_ids_applied` lists the GWP rules that changed a claim's wording (empty for copies). Internal cross-references are `{{ref:<source id>}}` tokens; external document IDs stay as written.
  - A reviewer's slot edit (`PATCH .../sections/{section}/slots/{slot}`) saves a new version with `origin: human` and moves the job to `MANUALLY_EDITED`. The deterministic draft checks are a `quality_report` with scope `drafts`.
- **Quality reports.** `quality_report` artifacts, by scope:
  - `section_plan`, `slot_plan`, `drafts`: the checks of each stage's output (Phases 7–9).
  - `validation`: one per validation round (Phase 10): the deterministic validator (structure, traceability, preservation, order, formatting, callouts, GWP style) plus the critic's findings.
  - no scope: the job's quality report, written by QUALITY_REVIEW and by a reviewer's resolution; `GET /validation` returns it. It adds required-slot gaps (`missing_slot`, one per gap marker), missing required sections, `high_risk_units` (unit → `RiskTag`s from the deterministic risk classifier) and `gate_counts` (open issues per hard gate, zeros included).
  - `ValidationIssue.source`: `deterministic`, `critic` (advisory: never carries a gate of its own; its message starts with `[critic:<problem>]`) or `human`. An open medium-or-worse deterministic issue, or a high critic one, on a high-risk unit gets the `high_risk_unresolved` gate and the suffix `[high-risk: <tags>]`.
  - Issue IDs are stable hashes of what was found. A reviewer resolves an issue (`POST /issues/{issue_id}/resolve`, a new report version with `resolved` and `resolution_note`); the resolution carries over to later rounds by issue ID. Resolving a `missing_slot` issue accepts the gap as N/A.
  - Soft scores: `unit_coverage`, `gwp_style_compliance` (share of checked claims with no deterministic STY violation), `passive_ratio`, `flesch_reading_ease`, `fk_grade`, `reworded_share`. Low issues never hold a job back.
  - Final status: HUMAN_REVIEW_REQUIRED while a gate issue or a high or critical issue is open; COMPLETED_WITH_WARNINGS while a medium issue is open, or no rendered document passed the post-render check; else COMPLETED.
- **Repairs.** A repaired section is a new `SectionDraft` version with `origin: repair` (claims renumbered, back in source order). `SectionState.attempts` counts repair rounds; `needs_review` marks a section a reviewer must settle.
- **Assembly** (Phase 12). After validation, ASSEMBLING writes the whole-document model:
  - `NumberMap` (`number_map`): one entry per chapter present (`kind: section`; an optional chapter with no content is listed in `sections_removed` and the ones after it move up), per source sub-heading kept in a chapter (`kind: heading`, "6.1.2"), and per referenced passage (`kind: unit`: its holder's number, plus `item_number`, the "No." cell of a list row or its step). A source section with no heading of its own gets its holder's number with `exact: false` and a `note`. `bookmark` is the hidden Word bookmark (`_Ref_<id>`) the renderer puts on that heading.
  - `AssembledDocument` (`assembled_draft`): the chapters present in order, the draft version of each section it was built from, and one `ResolvedRef` per `{{ref:...}}` token and claim: the source phrase, the text the document shows (`number` at `number_at` is a Word `REF \w` field to `bookmark`), and `status` `resolved`, `merged` (medium issue: check the wording) or `unresolved` (high `broken_cross_reference` gate; the phrase stays as written). Relative phrases ("described above") are checked: the passage just before (or after) in the source must still be on that side (medium issue).
- **Reconciliation** (Phase 12). RECONCILING checks the whole document (abbreviations, role names, numbering; issues tagged `[reconcile:…]`) and, with a GWP (`reconcile_llm`), asks the LLM once for contradictions, duplicates and terms named two ways. A proposed patch of a reworded claim is applied only if the whole document still validates; the patched section is a new `SectionDraft` with `origin: reconcile`, and the stage writes the next `validation` round.
- **Rendered document** (Phases 11–12). RENDERING renders the assembled document into a copy of the template's normalized file: a `docx` artifact (the review draft) and a `render_report`. When the template does not number its headings, references show the numbers as plain text (a warning) and the numbering check is skipped.
  - `RenderReport.problems` also lists a chapter or heading that Word numbers differently from the `NumberMap`, and a REF field without its bookmark.
  - `RenderReport.slots`: one `SlotRender` per template slot, with `outcome` `filled`, `gap_marker` (review draft: "Source content not found. Human review required."), `removed_empty` (optional slot without content, removed with its instruction), `removed_accepted_gap` (final export) or `no_anchor`.
  - `regions`: each conditional region `choice_applied` (with `choice`), `kept` or `removed` (with a `note`); `sections_removed`: optional sections without content.
  - `warnings` are for the reviewer (a source table written in its own columns, a doubtful column match, a missing figure file); `problems` are post-render check failures (a claim not in the document, blue instruction text left, headers changed...). `ok` is true when there are none.
  - `toc_entries`: the entries written into the table of contents (one per heading the TOC field's switches include, with its Word number, a hyperlink and a `PAGEREF` to a bookmark on the heading). `toc_page_numbers`: true when Word measured the page numbers (`render_toc_pages: word`, Word installed); then the document does not ask Word to update fields on opening. Otherwise the entries have no page number until Word updates the fields, and the document asks for that.
  - `RenderMode.final` removes reviewer-accepted gaps and unwraps the slot content controls; it is refused while any gap is unresolved (`RenderBlocked`). The review draft keeps the controls.
- **Export and audit** (Phase 12).
  - `GET /{id}/export/word` renders in `final` mode with the accepted gaps (resolved `missing_slot` issues) removed, and saves `docx`, `render_report`, `number_map` and `assembled_draft` with `scope: final`, plus a `traceability` artifact. It is refused (409) unless the job is `COMPLETED` or `COMPLETED_WITH_WARNINGS`. Each export is an `export` event, each removed gap a `gap_removed` event; the SOP record gets `migrated_path`, `migrated_job_id` and `migrated_at`.
  - `GET /{id}/traceability` (`format=json|csv`): one `TraceRow` per claim and cited passage (claim number, text with references resolved, the passage, its source section and location; `part` when the passage was split).
  - Every LLM call is an `llm_call` event: `stage`, `task`, `prompt_version`, `model`, `input_tokens`, `output_tokens`, `latency_ms`, `retry`, `status` (`ok`, `error`, `unparseable`), `unit_ids` and `claim_ids` supplied (`audit.py`). A reviewer's slot edit is a `slot_edited` event with the claim-by-claim diff; a plan edit is a `plan_edited` event; the actor is the reviewer.
- **Job statuses.** `RENDERING` (Phase 12) sits between `RECONCILING` and `QUALITY_REVIEW`. `DraftOrigin.RECONCILE` marks a reconciliation patch.
- **Language.** `SourceDocument.language` is the hook for translation later; claims are the unit of translation.

## Changing a contract

1. Change the model in `app/schemas/v2/`.
2. Update the matching example in `examples/`.
3. Run `pytest tests/test_v2_contracts.py`.
4. Note the change in `PROGRESS.md`, because later phases depend on these shapes.

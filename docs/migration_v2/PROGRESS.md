# Migration v2: Progress Log

**Tasks waiting on the user, and how to check progress: see [USER_TASKS.md](USER_TASKS.md).**

Handoff log for `IMPLEMENTATION_PLAN.md`. Append one entry at the end of each phase or session. At the start of a session, read the plan's relevant phase and the latest entry here.

Entry format:

```
## YYYY-MM-DD: Phase N (name): done / partial
- Done:
- Files touched:
- Open issues / decisions:
- Next step:
```

---

## 2026-10-02: Planning: done
- Done:
  - Reviewed the codebase and recorded the problems in `docs/KNOWN_ISSUES.md`.
  - Reviewed `docs/migration_plan.md`.
  - Approved this phased plan.
- Decisions:
  - Templates will be normalized with content-control anchors.
  - GWP arrives as a Word/PDF document, curated into a rules JSON.
  - v2 runs alongside the existing `/documents/migrate`.
  - The frontend review UI is in scope.
  - Cross-references are drafted as `{{ref:...}}` tokens and resolved after whole-document assembly, before rendering.
- Next step:
  - Phase 0. Collect the sample SOP, template and GWP files into `tests/fixtures/migration_v2/` (or the gitignored `data/samples/` if confidential).
  - Then fix the `list_records()` bug and the relative template path in `app/api/migration.py`.

## 2026-10-02: Phase 0 (code part): done; samples pending
- Done:
  - Branch `features/migration-v2` created from `main`.
  - Local `venv/` created and `requirements.txt` installed. Baseline: 126 passed, 4 skipped.
  - Fixed `template_store.list_records()` → `get_records()` (2 calls) in `app/api/migration.py`. This had broken template lookup by name and the default-template fallback.
  - The template fallback now uses `settings.template_upload_dir` instead of a hard-coded path.
  - **Model routing bug fixed.** LangChain read `gemini/gemini-2.5-flash` as provider `google_vertexai`, with the whole string as the model name. Added `resolve_model()` in `app/services/llm/chain_factory.py`, which maps the `provider/model` prefixes in `.env` (`gemini`, `openai`, `anthropic`) to LangChain providers. The `.env` format is unchanged. `langchain-google-genai` reads `GEMINI_API_KEY` directly.
  - Created `tests/fixtures/migration_v2/{sop,template,gwp,golden}/` and a README with the golden-expectation format. Confidential files go in `data/samples/`, which is already gitignored via `data/`.
- Files touched: `app/api/migration.py`, `app/services/llm/chain_factory.py`, `tests/test_chain_factory.py`, `tests/fixtures/migration_v2/`.
- Open issues:
  - Sample SOP, template and GWP files are not available yet. Golden expectations and the extraction spike are still to do.
  - `tests/test_template_management.py` writes to the tracked `data/template_records.db`. Restore it with `git checkout -- data/template_records.db` before committing, or fix the test to use `tmp_path`.
  - `Settings` paths (`data/...`) are still relative to the working directory, so run the server and tests from `content_extraction/`.

## 2026-10-02: Phase 1 (v2 data contracts): done
- Done:
  - `app/schemas/v2/`: `common`, `source`, `template`, `gwp`, `plans`, `draft`, `quality`, `job` and `refs`, exported from `app.schemas.v2`.
  - Models enforce the plan's rules: claims must cite sources, split needs unit IDs, omit needs a justification, a template can't be `ready` with unanchored slots, and gate issues block completion. See `CONTRACTS.md`.
  - `docs/migration_v2/CONTRACTS.md`, plus one example JSON per model in `docs/migration_v2/examples/`.
  - `tests/test_v2_contracts.py` (29 tests). Full suite: 161 passed, 4 skipped.
- Decisions:
  - `SourceDocument.cross_references` holds the detected references. Drafts carry `{{ref:<source_id>}}` tokens, and `NumberMap` resolves them at assembly time.
  - A `paragraph` (proximity) anchor never counts as ready.
  - `GwpRuleSet.select()` returns approved rules only, unless asked otherwise.
  - `SectionSlotPlan.unplaced_unit_ids` records units that fit no slot.
- Next step:
  - **Phase 2a** (no samples needed to start): traverse block-level `w:sdt` in `app/services/parser/docx_parser.py:79` and read `w:numPr`/`w:ilvl` for list levels (`:277`), using synthetic `.docx` built in tests.
  - Phases 3, 4 and 5 can start in parallel. Phase 5 (job store, orchestrator, API) needs no samples at all.

## 2026-10-03: Phase 0 (spike on real samples): done; samples not yet filed
- Done:
  - Received 3 SOPs and 1 template in `documents/` (`SOPs/028-BIS-00493.docx`, `SOPs/028-BIS-00535_1.0_...RPAS.docx`, `SOPs/BI-VQD-10505-S.docx`, `Templates/Template Main GP Docs.docx`). These are internal BI documents and should be treated as confidential. `documents/` is untracked but **not gitignored**. No GWP document yet.
  - Ran the current v3.1 pipeline (parse, tables, icons, captions, AST, `MigrationExporter`) on all 3 SOPs and diffed the output text against every `w:t` in the body.
- Spike results:

  | SOP | Word recall | Sections exported | Paragraphs missing |
  |---|---|---|---|
  | 028-BIS-00493 | 91.2% | 1 (`0 PREAMBLE`) | 18, all TOC |
  | 028-BIS-00535 (RPAS) | 90.9% | 1 (`0 PREAMBLE`) | 47, nearly all TOC |
  | BI-VQD-10505-S | 88.4% | 1 (`0 PREAMBLE`) | 20 (15 TOC, 3 cover-sheet fields, 2 table cells) |

- Gaps found, by severity:
  1. **Section detection fails on all 3 SOPs.** Every element lands in `0 PREAMBLE`. `_is_major_section_heading` (`migration_exporter.py:44`) needs a literal `"<n> TITLE"` prefix at level 1. Real SOPs number their headings with Word auto-numbering (`w:numPr` on the Heading style), so the text is just `PURPOSE`. The AST also reports `Heading 1` as level 2. Fix in Phase 2a: derive section numbers from heading style and numbering instead of the text.
  2. **Content controls (`w:sdt`).** 13 to 30 per SOP. Most hold the TOC (boilerplate, fine to drop, but it should be flagged rather than silently lost). Cover-sheet metadata inside `tbl/sdt` is lost, including the Hierarchy Level and Type values, scope ("Cross-divisional") and the document title in BI-VQD-10505-S.
  3. **Metadata extraction is wrong for BI-VQD-10505-S:** `document_number` is missing, `document_version` is `"."` and `document_type` is `">"`.
  4. **Lists.** There are 19 to 39 `w:numPr` paragraphs per SOP but only 0 to 3 `list` elements are exported. Numbered items appear as plain paragraphs with no level.
  5. A few table cells are missing (BI-VQD-10505-S: one "Initiating the shipment request ..." cell and a "Governance and Procedure > SOP" cell). Check this against continuation and merged cells in Phase 2b.
  - The template has 17 tables, 14 drawings (icons) and only 1 `w:sdt`. Slot anchors will mostly come from blue instruction paragraphs and table pairs (Phase 3, priorities 4 and 5), not from content controls.
- Open issues / decisions:
  - Move the samples to `data/samples/{sop,template}/` (gitignored), or gitignore `documents/`, before the next commit.
  - The GWP document is still needed for Phase 4.
  - Golden expectations are not written yet.
- Next step: Phase 2a, with section detection (gap 1) as the first fix. Add a regression test that the 3 sample SOPs produce their Heading 1 sections (PURPOSE ... DOCUMENT HISTORY).

## 2026-10-03: Template callout colours (contracts + Phase 3 palette): done
- Done:
  - **Contracts.**
    - `common.py`: `CalloutKind` (`introduction`, `explanation`, `attention`, `key_takeaway`) and `HexColor`.
    - `template.py`: `CalloutStyle`, `CalloutLayout`, `CalloutOrigin`, `TemplateModel.callout_palette`, `TargetSlot.callout_kind`, `TargetSection.optional_marker`.
    - `plans.py`: `CalloutAssignment`, `AssignmentOrigin`, `SectionSlotPlan.callout_assignments` and `callout_kind_for()`.
    - `draft.py`: `Claim.callout_kind`.
    - `source.py`: `TableCell.fill_hex`.
    - Updated the examples and `CONTRACTS.md`, and added 7 contract tests.
  - **`app/services/migration_v2/template/callout_palette.py`.** The sample template gives exactly 4 entries: `D2F2F7` introduction, `00E47C` explanation, `F5CDB9` attention and `FBF9AA` key_takeaway.
    - The legend is table 6, and the prototypes are tables 10–13 under PROCESS.
    - It gives no false positives from the RACI matrix.
    - "distribution of controlled prints/copies (optional)" is detected as optional.
    - `tests/test_v2_callout_palette.py` has 16 tests; the sample-template test skips when the file is absent.
  - Full suite: 184 passed, 4 skipped.
- Decisions:
  - v2 only; v1 `callout_builder.py`, `docx_migrator.py` and `template_slimmer.py` are unchanged.
  - Callout kinds come from fixed template slots and from reviewed promotions, never from LLM-chosen colours.
  - Table indices in `callout_palette` count top-level body tables, including those inside content controls; the TOC is skipped.
- Notes:
  - The sample's grid widths come from `w:tblGrid` direct children only. A tracked `w:tblGridChange` holds the old grid.
  - The sample template's TOC entries have no style and sit in a `docPartGallery="Table of Contents"` content control.
- Next step: the slot planner, drafter and renderer pieces are written into Phases 8, 9, 10, 11 and 2b of `IMPLEMENTATION_PLAN.md`. Phase 2a section detection is still the next build step.

## 2026-10-03: Phase 0 (samples and golden drafts): done, pending SME review
- Done:
  - Added `documents/` to `.gitignore`. The samples stay there: `SOPs/`, `Templates/`, `golden/`, and `GWP/` once the guide arrives.
  - Drafted the golden expectations `documents/golden/<sop stem>.json` for all 3 SOPs. They hold:
    - the section mapping, including the optional template section left empty;
    - must-preserve facts (91 quoted strings, all checked against the SOP text);
    - slot expectations by `ContentType`;
    - cross-references with their expected targets;
    - callout candidates;
    - review points.
  - Updated `tests/fixtures/migration_v2/README.md` with the provenance, the new location and the extra golden fields.
- Findings while drafting:
  - BI-VQD-10505-S already follows the target template, so it is the one-to-one control case.
  - In 028-BIS-00535 (RPAS), SCOPE holds "Acceptance criteria for RPAS". These are prohibited use cases that belong in PROCESS. It is the `split` case, and the move shifts the PROCESS numbering, which tests the NumberMap.
  - In 028-BIS-00493, the references point to numbered entries ("see Chapter 7, no. 1") rather than to sections.
- Open: SME review of the goldens; the GWP document (user will add it).
- Next step: Phase 2a.

## 2026-10-03: Phase 2a (DOCX numbering, sections, content controls, lists): done
- Done:
  - New `app/services/parser/ooxml.py`:
    - `iter_body_blocks` flattens block `w:sdt` and skips the TOC control.
    - `element_text` includes inline `w:sdt` runs.
    - `NumberingResolver` reproduces Word auto-numbering: the paragraph's `numPr`, then the style chain (`basedOn`); `numId=0` turns it off; empty paragraphs still count; `startOverride`; decimal, letter and roman formats; bullets; `outlineLvl`.
  - `docx_parser.py`:
    - Walks block content controls.
    - Headings get `metadata.numbering` (Word number) and `numbering_source="numPr"`.
    - Any `numPr` paragraph is a list item with `list_level`, `list_number` and `num_id`. `TemplateDocxParser` sets `_NUMPR_LISTS = False`, so template output is unchanged.
    - Every paragraph gets `paragraph_index`.
    - Cell text includes inline content controls.
  - `metadata_extractor.py`: cover-sheet cells read through `element_text`. BI-VQD-10505-S now gives version 5.0, number BI-VQD-10505 and the full type.
  - `ast_builder.py`:
    - Headings take the Word number.
    - Lists take their type from Word numbering, and `nesting_depth` is the deepest level.
    - `SourceLocation.paragraph_index` is set.
  - `migration_exporter.py`:
    - A level-1 heading with a top-level Word number starts a section titled "N TITLE".
    - Subheadings are prefixed with their number ("6.1.2.1 …").
    - An unnumbered "Heading 1" gets a running number.
    - The text-based rules still apply otherwise.
  - Tests: `tests/test_docx_numbering.py` (8, synthetic) and `tests/test_sample_sops.py` (14, real SOPs against the reviewed goldens; it skips if they are absent). It checks:
    - sections against the golden mapping;
    - **every computed heading number against the document's own TOC** (75 entries);
    - cross-reference targets;
    - all `must_preserve` facts;
    - BI-VQD metadata;
    - the RPAS 7-item prohibited list.
  - Full suite: 206 passed, 4 skipped.
  - The goldens are marked `reviewed`.
- Spike re-run: each SOP now exports `0 PREAMBLE` (cover sheet) plus its 9 chapters, where before everything was in `0 PREAMBLE`. The paragraphs still reported missing are only the TOC (dropped on purpose) and two cells whose manual line break is now a space (the text is present).
- Decisions:
  - List items stay flat, with `metadata.list_level`, instead of nested `children`, because the v3.1 exporter reads only `ListNode.items`. The v2 source-unit exporter should use `list_level`.
  - Section titles keep the "N TITLE" form, so the `"0 "` preamble skip and `docx_migrator`'s subsection merge keep working.
- Open issues:
  - 028-BIS-00493's cover sheet says version 2.0, but its history table has a 3.0 row. The source document is inconsistent, so it is left as extracted.
  - The heading "2\tAPPLICABILITY" text contains a leading tab in some SOPs. It is stripped by `.strip()`, but keep this in mind for the v2 unit text.
- Next step: Phase 2b, the `source_unit_exporter.py` that turns the AST into `SourceDocument` (units, `TableCell.fill_hex`, warning/note typing, cross-reference detection). It can be checked against the goldens' `slot_expectations` and `cross_references`.

## 2026-10-03: LLM provider: Azure OpenAI gpt-4o: done
- Done:
  - `Settings` reads the standard Azure names as well as the `SOP_` ones; the `SOP_` name wins:
    - `AZURE_OPENAI_ENDPOINT`
    - `AZURE_OPENAI_API_VERSION`
    - `AZURE_OPENAI_DEPLOYMENT_NAME`, shared by the planner and the summarizer.
  - `use_azure_openai` now defaults to "on when an endpoint and key are set". An explicit `SOP_USE_AZURE_OPENAI` still wins.
  - The user's `.env` (git-ignored) resolves to Azure on, deployment `gpt-4o-poc` for both roles, API version `2024-12-01-preview`. The `.env` was not modified.
  - Tests:
    - `tests/test_chain_factory.py` gained 4 offline Azure settings tests, isolated from the real `.env`.
    - New `tests/test_llm_live.py` (marker `llm`, registered in `pytest.ini`) runs only with `SOP_RUN_LLM_TESTS=1`.
    - Live run: plain call and structured output (`with_structured_output`) both passed against `gpt-4o-poc`.
  - Full suite: 210 passed, 6 skipped (2 of them are the live tests).
- Notes:
  - `settings.py` calls `load_dotenv()` at import, so tests that need a clean config must `monkeypatch.delenv` the Azure variables. `_env_file=None` alone is not enough.
  - The `.env` also has `AZURE_OPENAI_IMAGE_*` (gpt-image) entries. Nothing in the app uses them.
  - Run the `@pytest.mark.llm` tests from Phase 7 onwards with `SOP_RUN_LLM_TESTS=1 pytest -m llm`.

## 2026-10-03: Golden refinement (icon slots): done
- **Icon audit.** The template has 14 icons. 6 are instruction icons: PURPOSE has 2 rows, APPLICABILITY has 4. The other 8 are callouts, already in the palette. The source icon files differ from the template's (different md5), so slots must be matched by row position and instruction, never by icon image.
- **Goldens.** `slot_expectations` now name the exact slot (`target_slot_ref`, e.g. `APPLICABILITY.geography`). Also added: `expected_empty_slots`, `figures` and `source_icon_rows`. The slot names are documented in `tests/fixtures/migration_v2/README.md`. Every new quote was checked against its SOP.
  - BI-VQD-10505-S: its single PURPOSE icon row splits into PURPOSE.what and PURPOSE.intention.
  - RPAS: "AI bots not in scope" goes to `APPLICABILITY.not_covered`, which has no icon.
- **Correction.** The 3 yellow (`FFFF00`) RPAS cells are the header row of the naming-convention table, not meaningful risk colours as stated in earlier entries. Header fills are dropped.

## 2026-10-03: Phase 2b (v2 source units): done (DOCX)
- Done:
  - New `app/services/export/source_unit_exporter.py`, AST → `SourceDocument`:
    - **Sections:** nested via `parent_id`, with Word numbers as IDs (`SRC-6.1.2.1`). The preamble is `SRC-0`.
    - **Paragraph typing:** warning, note and caption by prefix.
    - **List items:** procedure step or bullet, with `list_level` and `list_number`.
    - **Table rows:** `table_row`, `definition` or `reference`, with `header_cells`. `fill_hex` is kept except grey or header fills.
    - **Icon tables** (image-only cell beside text) give one paragraph unit per paragraph, with the icon as an asset.
    - **Figures** become `figure` units, with `caption` units linked by `caption_of`.
    - **Relations:** warning/note → `warning_for`/`note_for`.
    - **Boilerplate:** the preamble and history/approval sections.
    - **`content_hash`** is keyed by section heading and occurrence.
  - New `app/services/export/source_refs.py`: section, "Chapter N, no. M" (resolved to the numbered row), step, figure, table, appendix, relative and external-document references.
  - Contracts: `UnitType.FIGURE`, `SourceAsset`/`AssetKind` and `SourceUnit.assets`. Only figure units may carry a figure asset.
  - Job: `job_manager` writes `data/output/{uid}_v{n}_units.json` (versioned) next to the unchanged v3.1 file. `sop_records.units_path` is an additive column. A units failure never fails the job.
- Shared fixes found on the way (affect v1 too):
  - **Icons moved to the end of the document.** `ASTBuilder._reorder_icons` appended every icon without a bbox (all DOCX icons) at the end. These icons now stay in place.
  - **Figures turned into icons.** `IconExtractor` judged by the image file's pixel size. A figure displayed large is now never an icon: the parser records the displayed size from `wp:extent`. The icon also keeps its metadata.
  - **Inline icons** stay with the text in their own paragraph (`paragraph_index`).
  - **Cells inside row-level content controls** were skipped by the table parser and the metadata extractor. They are now read: the 00493 and RPAS cover "Type" value, now `STANDARD OPERATING PROCEDURE`.
- Results on the samples:
  - **00493:** 22 sections, 148 units, 3 figures with captions, 17 references resolved.
  - **RPAS:** 45 sections, 186 units, 2 figures, 4 resolved plus 12 external.
  - **10505:** 15 sections, 95 units, 5 icon rows (PURPOSE 1, APPLICABILITY 4), 1 resolved plus 16 external.
  - **100% of body words** (TOC excluded) are present in each `SourceDocument`.
- Tests:
  - `tests/test_source_unit_exporter.py` (13, synthetic).
  - `tests/test_units_job_output.py` (2: end-to-end job, column migration).
  - `tests/test_sample_sops.py` +18. It covers zero lost words, slot evidence present, icon rows as metadata, figures with captions, golden cross-references resolving to the expected heading or row, and boilerplate.
  - Full suite: 243 passed, 6 skipped.
- Not done / open:
  - The PDF path runs through the same exporter, but there is no PDF sample, so it is untested and has no lower-confidence list-level flag.
  - KNOWN_ISSUES #5 (v3.1 `{uid}_v2.json` overwritten on re-upload) is deferred. Five readers use that fixed name; the units file is versioned already.
  - The suite also modifies `data/sop_records.db` (KNOWN_ISSUES 5a).
- Next step: Phase 3 (template slots and anchors). Turn the 6 icon rows and the "not covered" paragraph into slots using the `target_slot_ref` names, wire the callout palette prototypes in as fixed callout slots, then normalize. Phase 5 can run in parallel.

## 2026-10-03: Phase 3 (template slots, anchors, normalization): done
- Done:
  - **Slot detection** (`migration_v2/template/slot_detector.py`, `detect_template_model`). It follows the priority order, working directly on the OOXML:
    - content controls `CC_<SECTION>_<KEY>`;
    - bookmarks `SLOT_<SECTION>_<KEY>`;
    - placeholders (`[Insert …]`, `<<…>>`);
    - icon-row tables (one slot per row, icon as `SlotIcon` metadata);
    - data tables (header plus example rows; a `[Role n]` header row is skipped);
    - callout prototypes (fixed callout slots);
    - blue instruction groups. A group that leads into a table becomes that table's instruction (`hide_after_population`), with fixed text in between allowed. Otherwise the group is a paragraph slot (`replace`). The paragraph just before a callout box is that box's instruction.
  - Each slot gets a `content_type` (section heading, then instruction keywords), an `instruction_behavior`, a `formatting_profile`, and `required` (section flag; "if applicable / where possible / optional" in the opening line makes it optional, "insert (None)/N/A" keeps it required).
  - **Contracts:** `TargetSection.key` and `TargetSlot.key` (optional, unique), plus `TemplateModel.slot_by_ref("APPLICABILITY.geography")`. Slot IDs are `TGT-<n>-<KEY>`.
  - **Ambiguous regions** are reported, never guessed: black text with an inline blue choice ("This Directive/SOP/...:") and tables written entirely in blue. A config decides them by region ID (`p:<n>`, `t:<n>`) as `fixed`, `instruction` or `slot`.
  - **Overrides** (`template/overrides.py`): `TemplateConfig` from `data/template_config/{uid}.json` (`settings.template_config_dir`), covering `callout_palette`, `regions`, `sections.required`, and `slots` by ref. Unknown refs are reported.
  - **Normalizer** (`template/normalizer.py`): writes a copy with each destination wrapped in a tagged content control. Paragraph groups get a block control, icon/callout text cells a control inside the cell, and data-table example rows one row-level control. The original is untouched, and region IDs and body text are unchanged.
  - **Readiness** (`template/readiness.py`): `ReadinessReport`. Paragraph-only or missing anchors give `needs_normalization`. Undecided regions, icon rows without instruction, callouts without a prototype and unknown config refs give `needs_review`. Palette and highlight notes are warnings only.
  - **Service** (`template/service.py`): build, normalize and save. It writes `template_output_dir/{uid}_v{n}_normalized.docx`, `_model.json` and `_readiness.json`. The model is rebuilt from the normalized copy (so anchors come from the tags) and re-validated. `link_instruction_ids` fills `instruction_ids` from the v2.0 extraction output when it exists.
  - **API:** `POST /templates/{id}/normalize`, `GET /templates/{id}/slots`, `GET /templates/{id}/readiness`, and `GET`/`PUT /templates/{id}/slot-config`. A PUT re-assesses the saved model. Before normalization, `slots` and `readiness` are computed on the fly and not saved.
  - **Store:** `template_records.model_path`, `normalized_path` and `readiness_status` (additive migration). Delete removes the model, readiness and normalized files.
  - **Refactors:** `TemplateDocxParser.is_blue_hex` (shared blue rule). The palette helpers are public (`block_text`, `icon_key_for`, `is_heading`, ...), and `palette_from_document` runs on an open document.
- Results on the sample template (`Template Main GP Docs.docx`):
  - **Sections:** 10, numbered by Word. Chapter 9 "distribution..." is optional, so DOCUMENT HISTORY is 10 in the template.
  - **Slots:** 21. The 6 icon rows are `PURPOSE.what`/`intention` and `APPLICABILITY.roles`/`units`/`geography`/`processes`. `APPLICABILITY.not_covered` has no icon. There are 4 callout slots in PROCESS, and 7 table slots: definitions terms/abbreviations, roles table A/B (`roles`, `raci`), associated documents, references, history. The paragraph slots are IMPLEMENTATION, PROCESS and DISTRIBUTION content.
  - **Golden refs:** every `target_slot_ref` and `expected_empty_slots` ref in the 3 goldens resolves.
  - **Ambiguous regions:** 3. Two are the "This Directive/SOP/Work Instruction/Guidance..." lines (p:26 PURPOSE, p:32 APPLICABILITY); the black text is kept and the blue part is the document-type choice. The third is the all-blue competence table (t:7).
  - **Readiness before normalization:** `needs_review`, with 4 paragraph anchors and 3 undecided regions.
  - **After normalization** with `documents/template_config/Template_Main_GP_Docs.json` (p:26 fixed, p:32 fixed, t:7 instruction): `ready`. All 21 slots are content controls, re-detection gives the same slots, and the body text is identical.
- Tests:
  - `tests/test_v2_template_slots.py` (17, synthetic): content controls, bookmarks, placeholders, icon rows, merged-cell table, callout, ambiguous regions and decisions, normalization round trip and row-level control, readiness transitions, icon row without text, overrides and unknown refs, config file, instruction-ID linking, store column migration, API end to end.
  - `tests/test_sample_template_model.py` (7, sample).
  - Full suite: 267 passed, 6 skipped.
- Open / decisions for the user:
  - **The 3 region decisions in `documents/template_config/Template_Main_GP_Docs.json` are Claude's suggestion and need review.** In particular, is the competence table guidance to delete, or content to keep?
  - **Inline doc-type choice:** "This Directive/SOP/Work Instruction/Guidance:" needs the renderer (Phase 11) to replace the blue choice with the document's type ("This SOP:").
  - **Untested in Word:** the normalized `.docx` has not been opened in Word here (no Word or LibreOffice on this machine). The text and structure checks pass, but it should be opened once in Word.
  - **Required flags** are heuristic. ROLES tables A/B both come out required, although the template says "use A, B, or both"; fix this in the config if wanted.
  - **Deferred:** the optional LLM `content_type` classification, kept as keyword rules only.
- Next step: Phase 4 (GWP rules) once the GWP document is in `documents/GWP/`. Otherwise Phase 5 (migration job store, orchestrator, API), which needs only Phase 1.

## 2026-10-03: Phase 3 follow-up (user decisions, Word check): done
- Decisions from the user:
  - The "This Directive/SOP/..." lines and the competence table are **chosen from the SOP's content**.
  - Roles tables A and B are **either or both**.
- Done:
  - New region decision `conditional`. The region becomes a `ConditionalRegion` on its section (`inline_choice` or `block`) and is wrapped in a `COND_<SECTION>_<n>` content control. Readiness requires every conditional region to be anchored. The competence table is no longer folded into table A's instruction.
  - New `TargetSection.one_of`, set by config `sections.<KEY>.one_of`. Each slot in a group becomes optional; a later validator checks at least one is filled.
  - Sample config (`documents/template_config/Template_Main_GP_Docs.json`): p:26, p:32 and t:7 are conditional; `ROLES.one_of = [["roles", "raci"]]`.
  - **Checked in Word** (COM, via `scripts/word_resave.ps1`):
    - The normalized sample opens without repair: 11 pages and 1,536 words, the same as the original, with all controls present.
    - Bug found: Word splits a row-level control spanning several rows into its own table on save, which shifts table indices and breaks detection. Now each example row gets its own control with the slot's tag, and a Word-resaved copy detects identically (`ready`, same slots, anchors and conditional regions).
  - Tests: `tests/test_word_roundtrip.py` (2, opt-in `word` marker, `SOP_RUN_WORD_TESTS=1`), plus conditional and one-of tests. Full suite: 269 passed, 8 skipped.
- Open:
  - Phase 7/8 must decide each conditional region per migration.
  - Phase 10 must enforce the one-of rule.
  - Phase 11 must replace inline choices and remove unused conditional blocks and example rows.

## 2026-10-06: Phase 4 (GWP ingestion and rule catalog): done, pending human review
- Input: `documents/GWP/BI-VQD-24416-G.pdf`, "Good Writing Practice for Governance and Procedure Documents" v3.0 (15 pages, PDF). It is confidential, and `documents/` is gitignored.
- **PDF fix found on the guide** (`app/services/layout/pdf_layout_analyzer.py`):
  - The header/footer checks read `el.text`, but `ExtractedElement` has `content`. The text was always empty, so every non-table element in the top 14% or bottom 12% of a page was dropped as a running header or footer.
  - On the guide this lost the bullet 'Use personal pronouns like "I" and "you"' (just above a footer) and the first line of page 13.
  - Now: anything in the outer 6% band is a header or footer by position alone, as before. Further in, text must match a pattern, or repeat in the zone on at least half the pages (`_find_running_texts`). The repeat check catches the title printed beside "Title:".
  - Result: 119 rule-input units. Every line of pages 3 to 14 is present, apart from spacing differences such as `6.5 .`.
  - Tests: `test_body_text_near_margin_is_kept`, `test_repeated_header_text_is_stripped` in `tests/test_ast_phase1.py`.
- **Contracts** (`app/schemas/v2/gwp.py`):
  - `RuleOrigin` (`guide`, `baseline`, `manual`) on `GwpRule`.
  - `GwpExtractionReport` (input units, uncovered units, skipped guidance, dropped candidates, downgraded checks).
  - Examples updated, plus a new `examples/gwp_report.json`. See CONTRACTS.md.
- **New package `app/services/migration_v2/gwp/`:**
  - `ingest.py`: `parse_guide` runs the SOP pipeline (parse, tables, icons, captions, AST, `SourceUnitExporter`) on a .docx or .pdf guide. `rule_input_sections` drops the cover page, TOC, references, associated documents, history and boilerplate.
  - `extractor.py`: `GwpRuleExtractor` makes one structured LLM call per batch of whole sections (16k chars; the sample takes 2). The output schema is `GwpExtractionOutput`, with params as a JSON string because strict structured output rejects free-form objects. `build_rule_set` then works deterministically:
    - drops candidates with no valid citation;
    - merges duplicates across batches;
    - downgrades invalid deterministic params to `semantic`;
    - numbers each category in guide order, skipping the baseline PRES IDs;
    - adds the baseline rules;
    - writes everything it changed into the report.
  - `baseline.py`: `PRES-001`..`PRES-006` (numbers, dates and durations, no invented roles, modality, conditions, document references). They are always approved and re-added if a reviewer deletes one.
  - `checks.py`: the deterministic check vocabulary (`forbidden_terms`, `max_sentence_words`, `passive_ratio`, `readability`, `callout_palette`, `modality`, `protected_values`, plus an optional `note`) and `check_params_problems`. Phase 10 implements the checkers.
  - `service.py`: files `data/gwp/{guide_id}_v{n}_{units,rules,report}.json`, plus `validate_rule_set`, `save_reviewed`, `approve_rules`, `load_active_rule_set` and `select_rules(rule_set, categories, content_types)`. With no approved guide, `select_rules` returns only the baseline rules.
  - Status flow: `uploaded` → `parsed` → `extracting` → `in_review` → `approved` (no candidates left). A save that leaves candidates moves the guide back to `in_review`.
- **Prompt:** `app/services/llm/prompts/v2/gwp_extractor.py`, `PROMPT_VERSION = "gwp_extractor/1"`. Process-only guidance (proofreading, tools, translation workflow, implementation dates) goes to `skipped` with a reason, so the reviewer sees full coverage.
- **Store:** `app/stores/gwp_store.py`, table `gwp_guides` in `data/gwp_guides.db` (version, status, paths, rule counts, prompt version, model, error). `get_active()` returns the most recently approved version.
- **API** (`app/api/gwp.py`, `/api/v1/gwp`):
  - `POST /api/v1/gwp` uploads and parses a guide.
  - `GET /api/v1/gwp`, `GET /{guide}` and `/{guide}/units` read guides. `?version=` selects a version; the default is the latest.
  - `POST /{guide}/extract` runs the LLM extraction.
  - `GET` and `PUT /{guide}/rules`: a PUT is validated, and errors come back as 422 `GWP_RULES_INVALID` with one field error per problem. A PUT also imports a hand-curated file.
  - `POST /{guide}/approve` takes `rule_ids`, or approves every candidate when none are given.
  - `GET /{guide}/report`, `GET /active/rules` and `DELETE /{guide}`.
- **Sample rule set** (not LLM-made yet): Claude hand-curated `documents/GWP/BI-VQD-24416-G_rules.json` and `_report.json` from the guide text.
  - 46 rules: 6 baseline, 13 STR, 19 STY, 1 guide PRES (`PRES-007`, don't oversimplify technical detail), 7 FMT. 12 rules are deterministic.
  - All 40 guide rules are `candidate` and cite their units. All 119 input units are cited or skipped with a reason.
  - The rules carry the template slot refs (`PURPOSE.what`, `APPLICABILITY.roles`...) and the callout palette (#D2F2F7, #00E47C, #F5CDB9, #FBF9AA, matching the template).
  - It imports through the API: `PUT` gives `in_review` with 6 approved and 40 candidates.
- Tests:
  - `tests/test_v2_gwp.py` (32, synthetic, mocked LLM): check params, baseline, input selection, batching, post-processing, extractor prompts, validation, approval, selection, store, docx ingest and the API end to end.
  - `tests/test_sample_gwp.py` (5, sample): sections, edge-of-page text kept, running headers stripped, rule input, and the curated rules validate and cover the guide.
  - Full suite: 308 passed, 8 skipped.
- Open / decisions for the user:
  - **Human review of the 40 candidate rules** (Done-when). Approve them via `POST /api/v1/gwp/BI-VQD-24416-G/approve`, or edit the JSON.
  - **Conflict resolved by assumption:** the GWP says avoid "may" and use "must"/"should", but a migration must not change obligations. STY-009, STY-010 and STY-012 defer to `PRES-004`: "may" is flagged for the reviewer, never rewritten.
  - **Interpretations to confirm:** "two lines" means 30 words (STY-003). Readability uses the SOP band, Flesch ≥ 30 and grade ≤ 12 (STY-019). Passive voice stays under 10% (STY-002). All three are low or medium severity because the guide calls them targets.
  - **The LLM extraction has not been run on the real guide.** That would send the confidential guide to the configured Azure OpenAI deployment (`gpt-4o-poc`), and the earlier live tests deliberately sent no document content. Run it once that is approved, then compare with the curated set.
  - **The GWP itself (6.4) says to use only company-approved AI tools** ("iQGPT", "Microsoft 365 Copilot Chat") and that AI output always needs human review. Confirm that this pipeline's Azure OpenAI deployment counts as approved.
  - `pdf_parser.py:147` still prints `DEBUG pdf_parser: gap=...` to stdout on every wide span gap (pre-existing).
- Next step: Phase 5 (migration job store, orchestrator, API). Phase 6 (protected facts) can use `select_rules(..., [PRES])` and the `protected_values` and `modality` params.

## 2026-10-06: Phase 5 (migration job store, orchestrator, API): done
- **Auth:** `require_user` in `app/api/auth.py` is a reusable dependency, factored out of `GET /api/v1/auth/me`, which behaves as before. It guards `/api/v1/migrations` and, from now on, `/api/v1/gwp`. The older routers are still open (KNOWN_ISSUES #1). Tests override it with `app.dependency_overrides[require_user]`.
- **Store** (`app/stores/migration_store.py`, `data/migrations.db`), 4 tables:
  - `migration_jobs`: also holds the store-only `failed_stage` and `cancel_requested`.
  - `migration_sections`: rows are created once per job, and re-runs keep them.
  - `migration_artifacts`: unique on (job, kind, scope, version).
  - `migration_events`: every status change goes through `set_status`, which logs from and to statuses, the actor and a detail. `expected=` makes a transition conditional, so concurrent changes can't clobber each other.
  - `get_job` assembles the `MigrationJob` contract.
- **Artifacts** (`migration_v2/artifacts.py`): `ArtifactWriter.write` writes `data/migrations/{job_id}/{kind}[_{scope}]_v{n}.json` and never overwrites. It records the sha256, and `read` verifies it (`ArtifactCorrupted`). Setting `migration_v2_dir` controls the folder.
- **Inputs** (`migration_v2/inputs.py`): `InputResolver.resolve` runs when a job is created and refuses:
  - an unknown SOP (404) or a SOP without v2 units (409 `SOP_UNITS_MISSING`);
  - a template that is not `ready` (409 `TEMPLATE_NOT_READY`) or has no model file;
  - an unapproved GWP (409 `GWP_NOT_APPROVED`).
  With no GWP given, the job uses the active guide, else only the baseline rules (logged as a `baseline_rules_only` event).
- **Orchestrator** (`migration_v2/orchestrator.py`): a persisted state machine over `JobStatus`.
  - Review gates apply in `mode=review`, and `approve` writes a new plan version with `approved_by`.
  - A technical exception gives `FAILED_TECHNICAL`, with `error` and `failed_stage`.
  - Cancel is cooperative: it takes effect between stages, or immediately when the job is not running.
  - `retry(from_stage)` defaults to the failed stage. It refuses a stage whose input artifacts don't exist yet.
  - `resume_incomplete()` runs on startup: jobs mid-stage re-run that stage, and jobs waiting for a human stay paused. Shutdown cancels the tasks but leaves the statuses alone.
  - Each job runs as its own asyncio task, at most 2 at once. It holds an `LLMRateLimiter` and the `ChainFactory` for later phases.
  - Started and stopped in the `app/main.py` lifespan.
- **Stages** (`migration_v2/stages.py`): `StageContext` plus `default_stages()`.
  - PARSING is real. It snapshots the source model, template model and GWP rules as artifacts, and creates one section row per target section.
  - The rest are stubs, each logging a `stage_stub` event with its phase number:
    - PLANNING_SECTIONS writes a contract-valid section plan, with every source section `unresolved` / `needs_review`.
    - PLANNING_SLOTS writes a slot plan, with every slot `needs_review`.
    - DRAFTING to QUALITY_REVIEW only log the event.
  - A stubbed run ends at `COMPLETED_WITH_WARNINGS`, never `COMPLETED`.
  - Later phases replace a stage with `Orchestrator(stages={JobStatus.X: fn})`, or by editing `default_stages()`.
- **Plan checks** (`migration_v2/plan_checks.py`): every section, slot, unit and callout kind a human-edited plan names must exist. Phases 7 and 8 extend these into the full validators.
- **API** (`app/api/migrations_v2.py`, `/api/v1/migrations`):
  - `POST` (201), and `GET` with `?status`, `limit` and `offset`.
  - `GET /{id}`, which includes `running`.
  - `POST /{id}/cancel` and `/{id}/retry?from_stage=`.
  - `GET /{id}/audit`, `/{id}/artifacts` and `/{id}/artifacts/{kind}?version&scope`.
  - `GET`/`PATCH /{id}/section-plan` and `/{id}/slot-plan`, plus `POST .../approve`. A PATCH is allowed only while the job waits on that review, is validated (422), and is saved with `origin: human`.
  - `/{id}/validation` and `/{id}/traceability` return 404 until Phases 10 and 12. The slot-content PATCH returns 501.
  - Wrong-state requests return 409.
- **Real-sample check** (temp dir, not committed):
  - Inputs: the RPAS SOP units, the sample template normalized with its config (`ready`, 10 sections, 21 slots) and the curated GWP set (approved, 46 rules).
  - The job paused twice in review mode and ended `COMPLETED_WITH_WARNINGS`, with 7 artifacts. The stub section plan lists 37 source sections.
  - A fresh orchestrator resumed nothing, as expected for a finished job.
  - The local `data/*.db` hold no SOP or template records yet, so this ran outside the app.
- Tests:
  - `tests/test_v2_migration_jobs.py` (13): store and transition guard, artifacts and tamper detection, input checks, auto mode, guide snapshot, review gates, failure and retry, retry prerequisites, cancel (paused, running, finished), restart and resume, API auth (401 on both routers), the full API review flow, and the template-not-ready refusal.
  - 5 repeated runs were stable. Full suite: 322 passed, 8 skipped.
- Open:
  - No `GET /api/v1/export/word/{job_id}` yet (Phase 12).
  - Cancel waits for the current stage to finish; LLM stages should keep their calls short or check `cancel_requested` themselves.
  - Not run under `uvicorn` with a real login. The API tests use TestClient with the auth dependency overridden, plus one test without the override (401).
- Next step: Phase 6 (protected facts and deterministic preservation checks; no LLM). It reads `select_rules(..., [PRES])` and the `protected_values` and `modality` params, and runs as a stage after PARSING (`ArtifactKind.PROTECTED_FACTS`).

## 2026-10-06: Decision: GWP is an optional migration input
- From the user: a GWP is not necessary for a migration.
- Changed:
  - `InputResolver.resolve` no longer defaults to the active approved guide. A job uses a GWP only when `gwp_id` is given (it must be approved), and otherwise has `gwp_id = null`.
  - The PARSING stage still writes a `gwp_rules` artifact. Without a GWP it holds only the built-in preservation rules (`guide_id: "BASELINE"`), so later stages need no special case.
  - The event is renamed from `baseline_rules_only` to `no_gwp`, and it is informational, not a warning.
  - Docstrings for `GET /api/v1/gwp/active/rules` and `GwpStore.get_active` now say a migration still has to name the guide.
  - Tests: `test_gwp_is_used_only_when_named`, and `resolve` returns no GWP by default. Full suite green.
- **Open for Phase 9 (drafter):** what the drafter does without a GWP. Proposed: place and lightly normalize the source content (template structure, numbering, callouts from the template), with no style rewriting. The preservation rules and checks apply either way.

## 2026-10-06: Decision: no-GWP drafting is placement mode
- From the user: without a GWP, the SOP content goes into the template slots largely as written. Only the template's structure, numbering and callouts are applied. The built-in safety rules and checks apply either way.
- Recorded in IMPLEMENTATION_PLAN.md, Phase 9 ("Without a GWP"):
  - The default slot action is `copy_verbatim`.
  - No STY rewriting.
  - The only edits allowed are those needed to fit a slot: splits, list conversion, and dropping a heading the template already provides.
  - Citations are still required, and PRES and the Phase 6 and 10 checks run unchanged.
- Implications for earlier phases: the Phase 8 slot planner sets `migration_action: copy_verbatim` on mapped slots when the job has no GWP. Phase 10's critic does not score style for such jobs.

## 2026-10-06: Phase 6 (protected facts and deterministic preservation checks): done
- **Registry** (`app/services/migration_v2/quality/facts.py`, `extract_facts(source, job_id) -> ProtectedFacts`). Spans are claimed in priority order, so a date is never also read as numbers and a document ID never as a number.
  - **References:** document, form and system IDs (`BI-VQD-…`, `028-BIS-…`, ISO/ICH, `21 CFR Part 11`), e-mails and URLs.
  - **Cross-reference phrases** ("chapter 6.3", "Figure 2") are dropped, because the drafter writes them as `{{ref:…}}` tokens.
  - **Values:**
    - dates (ISO; numeric dates read day first);
    - frequencies ("annually" = "once a year" = "every 12 months"; "every 3 months" = quarterly);
    - durations and deadlines ("within" is a `<=` bound; written numbers; "a week" = 1 week; "a second" is not a duration);
    - percentages, numbers with units, and ranges ("2-8 °C" = "between 2 °C and 8 °C");
    - qualifiers (`>=` at least / minimum, `<=` at most / up to, `>` more than, `<` less than / under);
    - number words ("one" only before a noun-like word: not "one of", "no one").
  - **Obligations** per sentence:
    - must / shall / required / need to / "can only" give `mandatory`;
    - must not / may not / cannot / not permitted / never / "Do not" give `prohibition`;
    - should / recommended give `recommended`;
    - may / can / allowed give `permitted`;
    - an imperative sentence ("Approve the deviation.") is `mandatory`.
    Excluded: "if/as/where required", "can be found", the month "May", a noun start ("Review of …"), and a role start ("Process Owner approves").
  - **Roles:** first cells of tables whose first header is a role column (not deliverables tables), capitalized role phrases ("Process Owner", "Head of Quality …", but not "User Requirements Specification"), and role-like defined terms with their abbreviations.
  - **Terms:** "Full Term (FT)" when the initials match.
  - Excluded units: boilerplate and a table of contents (the PDF path does not mark the TOC as boilerplate).
  - `PreservationConfig.unit_aliases`: by default "working day" = "business day".
- **Comparators** (`quality/preservation.py`, `check_preservation(facts, source, drafts)`, or each one separately). They run per source unit, over all claims citing it, so content split across slots or an actor moved to the responsibility slot is fine.
  - `compare_values`, PRES-001 and PRES-002:
    - a missing value is critical, gate `numerical_change`;
    - a value a claim adds is high, gate `numerical_change`;
    - a missing value paired with an added one of the same kind becomes one "changed" issue showing both.
  - `compare_references`, PRES-006: missing gives gate `high_risk_unresolved`; added gives gate `unsupported_claim`.
  - `compare_modality`, PRES-004: making a plain statement explicit ("submits" → "must submit" or an imperative) is allowed. Any lost, weakened or strengthened modality, or a newly added should / may / must not, is flagged with gate `high_risk_unresolved` (critical when must or must not is lost).
  - `compare_roles`, PRES-003, with QA ≡ Quality Assurance via the terms: missing gives gate `high_risk_unresolved`; introduced gives gate `unsupported_claim`.
  - Issue IDs are stable hashes. Messages start with the rule ID. Units no claim cites are left to the Phase 10 coverage gate.
  - The checks run with or without a GWP.
- **Pipeline:** PARSING now writes a `protected_facts` artifact, with a `protected_facts` event giving the counts. The checks themselves run in VALIDATING once drafts exist (Phase 10).
- **Results:**
  - Table: all 21 allowed rewrites (including the §2.1 example and the worked example) raise nothing. All 21 disallowed ones raise the expected rule and gate.
  - Real samples (3 SOPs + GWP): 0 issues on verbatim and on allowed rewrites (shall → must, working → business days, "can be found" → "is found").
  - Seeded faults, all caught: 88/88 changed numbers, 83/83 edited references, 22/22 must → should.
- Tests: `tests/test_v2_preservation.py` (92): the rewrite table, extractor tables (including golden strings), terms and roles, the registry, config, multi-slot claims, the curated registry, and the real-sample checks. Full suite: 414 passed, 8 skipped.
- Known limits (by design, documented):
  - Values compare as sets per unit. A unit with "11" twice where one becomes "12", and "12" is also present, is not caught. Counting would false-alarm when the procedure and timing slots both repeat a value.
  - Modality compares the set per unit. Merging two "must" sentences into one is allowed; dropping one of two "must" obligations entirely, while another remains, is left to the critic (Phase 10).
  - "within 5 days" → "in 5 days" is flagged (a bound becomes a point in time). Equivalences beyond working/business days need configuration.
  - Role detection is heuristic and English-only, as is all extraction.
- Next step: Phase 7 (section planner).

## 2026-10-06: Phase 7 (section planner): done
- **Plan change agreed with the user:**
  - Planning uses names **and** content at minimum token cost.
  - Rules propose every mapping; one compact LLM call per job confirms or corrects it (the user chose "always confirm"). It no longer re-derives the plan from synopses.
  - IMPLEMENTATION_PLAN.md Phase 7 is rewritten. Live LLM runs on the 3 samples were approved.
- **Contracts:**
  - `MappingOrigin` (`rule` / `llm` / `human`) on `SectionMapping`; `PlanOrigin.RULE`;
  - `SectionPlan.prompt_version` / `model` / `token_usage`;
  - `TargetSection.aliases`, filled from the new `TemplateConfig.sections.<KEY>.aliases`.
  - Also: `ChainFactory.create_structured_planner(schema, include_raw=True)` for token usage, and `ChainFactory.planner_label()`.
- **Signals** (`migration_v2/planning/signals.py`, no tokens):
  - name score with built-in aliases (SCOPE → APPLICABILITY, PROCEDURE/Process → PROCESS, Attachments → ASSOCIATED_DOCUMENTS...);
  - per-unit content labels from table headers, unit types and keywords;
  - the Phase 6 extractors (prohibitions → restriction, imperatives / obligations → procedure);
  - list items inherit their intro line ("…must not be automated:" makes its bullets restrictions);
  - references to the document's own sub-documents (its number plus a suffix, e.g. `028-BIS-00535-RD00`) labelled `associated_document`.
- **Planner** (`planning/section_planner.py`):
  - rules: name-first for chapters; subsections inherit, move on content when the margin is ≥ 0.45, or are flagged [CHECK] at ≥ 0.30; close calls are flagged too;
  - prompt: one line per target and per section, previews only for [CHECK] / [MOVED];
  - corrections only; unit-level assignments are rebuilt into one_to_one, merge, split (with unit_ids), omit, unresolved and gap mappings;
  - safeguards against LLM errors: split without units, unknown keys or IDs, made-up unit IDs, moving or omitting a chapter whose heading names its target, no-op restatements.
  - A repair retry is paid only when an invalid change concerns one of the matcher's doubts.
- **Validator** (`planning/section_validator.py`) and **pipeline:**
  - the PLANNING_SECTIONS stage is real; it writes `section_plan` plus a `quality_report` (scope `section_plan`);
  - an LLM failure keeps the rule plan, with its doubts `needs_review`;
  - the orchestrator refuses section-plan approval while gate issues are open (`PLAN_HAS_GATE_ISSUES`);
  - `PATCH /section-plan` returns the validation and marks changed mappings `human`; new `GET /section-plan/validation`.
- **Fix found on the way:** Phase 6 read `028-BIS-00535-RD00` as `028-BIS-00535`. Document IDs now keep their suffixes.
- **Results on the samples:**
  - **Rules alone** match all 3 golden mappings: 028-BIS-00493 and BI-VQD-10505-S one-to-one throughout; RPAS SCOPE → APPLICABILITY with Acceptance criteria → PROCESS as a split; optional distribution removed; cover page omitted. Validation is clean, with 100% unit coverage.
  - **Live, gpt-4o-poc:** the plans still match the goldens and validate (final prompt `section_planner/4`; earlier prompt versions in 4 runs before it exposed the issues fixed above). On RPAS the LLM confirmed the split and the ASSOCIATED_DOCUMENTS close call. On the other two, its invalid changes on confident sections were dropped.
  - **Cost:** one call per SOP, about 1.4k–2.2k input and 50–180 output tokens; **about 5k input tokens in total for the 3 SOPs**. Prompt bodies are 2.5–5.2k characters.
- **Tests:**
  - `tests/test_v2_section_planner.py` (15): names and aliases, content labels, rule plan (one_to_one, merge, split, omit, gaps), unresolved blocking, validator cases, prompt compactness, mocked LLM (confirm, correct, unit split, repair, guard), stage usage and failure fallback, API approval gate, golden match on samples, and a golden negative.
  - `tests/test_v2_section_planner_live.py` (opt-in `llm`).
  - Phase 5 tests updated for the real planner. Full suite: 429 passed, 9 skipped.
- **Open:**
  - gpt-4o tends to propose unit moves for sections it has no previews for; they are dropped, but the prompt could be tightened further.
  - Thresholds (`AUTO_SPLIT_MARGIN`, `CHECK_SPLIT_MARGIN`, `LOW_CONFIDENCE`) are tuned on 3 SOPs only; Phase 14 calibrates them.
  - The planner never splits below subsection level on its own; unit-level splits come only from the LLM or a reviewer.
- **Next step:** Phase 8 (slot planner). It can reuse the same pattern: unit content labels against slot `content_type`s, an LLM confirm step, and the GWP-optional placement mode for `copy_verbatim`.

## 2026-10-06: Phase 7 follow-up: LLM flags instead of guessed passage IDs
- Problem: in the live runs, gpt-4o made up passage IDs (`unit_id_responsibility`, `unit_16`, section IDs as unit IDs) for sections it saw only as a one-line summary. The checker dropped those moves, and the model's suspicion was lost.
- Change:
  - `SectionPlanCorrections.flags` (`SectionFlag{source_section_ids, note}`) and prompt `section_planner/5`: "flag a section you saw no passages of instead of guessing unit IDs".
  - `apply` puts the note on the section, so the mapping becomes `needs_review` with "LLM: …" in its reason. The validator lists it with no gate; flags never move content or block approval.
  - A move dropped only for guessed unit IDs is turned into a flag automatically ("may hold passages that belong to X: <reason>"), unless the model already flagged that section.
  - The job event counts flags.
- Live (2 runs): plans still match the goldens.
  - 028-BIS-00493 PROCESS is flagged for possible scope content, and 6.3 for responsibility content.
  - BI-VQD-10505-S PROCESS is flagged for possible reference content.
  - Cost is unchanged: one call per SOP, about 5.4k input and 380 output tokens for the 3.
- Tests: 3 new (flag → review note without a gate, flags kept when changes are dropped, guessed move → flag). Full suite: 432 passed, 9 skipped.

## 2026-10-06: Inspection report (check finished stages on real SOPs): done
- Why: the user wants every finished stage checked on real SOPs before Phase 8, to avoid rework. They will provide PDF SOPs, more DOCX SOPs and a second template, and chose an HTML report.
- **CLI** `scripts/inspect_sop.py`, for a file or folder of .docx/.pdf. Options: `--template`, `--template-config`, `--gwp-rules`, `--golden-dir`, `--llm`, `--out` (default `documents/reports/`, gitignored).
  - One SOP failing does not stop a batch; its traceback goes to `_work/errors.log`.
  - Internal paths are short hashes, because the RPAS file name broke the Windows 260-character path limit.
- **Package** `app/services/migration_v2/inspection/`:
  - `runner.py`: parses the SOP, normalizes the template (falling back to detection only), and runs the real `stages.parsing` and `stages.plan_sections` on throwaway stores. There is no readiness gate, so a not-ready template can still be inspected.
  - `completeness.py`: text-loss check. DOCX compares body words against the units. PDF compares body lines minus running headers/footers and repeating watermarks. `tests/test_sample_sops.py` now uses it.
  - `golden.py`: `golden_problems` (moved from the planner tests) and the must-preserve check.
  - `checks.py`: the 7 checks.
  - `report.py`: the HTML page and index. Self-contained and escaped; values, roles and modal words are highlighted.
- **Found by the tool and fixed** (affects every BI-style PDF SOP):
  - The PDF path splits the cover into unnumbered pseudo-sections, "GENERAL INFORMATION" and "Table of Content". The section planner left their 15 units unresolved, which was a blocking issue.
  - Now everything before the first numbered chapter is omitted as front matter (with `skip_preamble_migration`), and a table of contents is omitted wherever it sits.
- **Results:**
  - 3 sample SOPs, rules only: 028-BIS-00493 and BI-VQD-10505-S all PASS. RPAS: all PASS except the section plan, WARN for the ASSOCIATED_DOCUMENTS close call that the LLM resolves.
  - With `--llm`: RPAS all PASS. The other two are WARN for the LLM's review notes. One call each, 1.5k–2.4k input tokens.
  - GWP PDF (PDF smoke test): text completeness PASS, plan WARN (review items only).
- Tests: `tests/test_v2_inspection.py` (5: stages and checks, escaping and highlighting, lost-text detection, index, samples). Planner test for PDF front matter. Full suite: 438 passed, 9 skipped.
- **Next (user):** add the PDF SOPs, the extra DOCX SOPs and the second template (with its config, or let the report list the regions to decide), run the tool, and review the reports. Every FAIL or WARN that is a defect gets fixed before Phase 8. Also pending: SME review of the goldens and approval of the 40 GWP candidate rules.

## 2026-10-07: Decisions and housekeeping
- Committed Phases 1–7 and the inspection tool as `9c75540` on `features/migration-v2` (not pushed). Before the commit: 438 passed, 9 skipped.
- From the user:
  - **Goldens accepted for now.** The 3 files in `documents/golden/` stand as the reference; the user will send changes if any come up.
  - **SOP content may be sent to the Azure OpenAI `gpt-4o-poc` deployment.**
  - **Empty slots.** Recorded in IMPLEMENTATION_PLAN.md, Phases 10, 11 and 12:
    - an optional slot with no content is removed;
    - a required slot with no content shows a gap marker in the review draft, and the reviewer accepts it as N/A or adds content;
    - the final export removes accepted gaps and their instruction text, and is blocked while any gap is unresolved;
    - nothing is ever generated to fill a gap.
- Added `USER_TASKS.md`: the user's open tasks and how to check progress.
- Next step: unchanged. The user adds the new SOPs and the second template, then runs the inspection; then Phase 8.

## 2026-10-07: Inspection WARN on BI-VQD-10505-S PROCESS: explained, fix deferred
- The user's `--llm` inspection run gave one WARN: "Mapping to 'PROCESS' needs review … content reference 8.1, other 7, scope 4; LLM: some passages in SRC-6 may belong to REFERENCES or APPLICABILITY".
- **False positive.** The plan matches the golden file, all passages are placed and nothing is blocking. Causes, both in `planning/signals.py` `unit_labels()`:
  - Procedure steps that cite another document inline ("as per BI-VQD-176305-S", "described in BI-VQD-10581-S") get the full `reference` label.
  - Bare `_SCOPE` words (`site`, `applicable`, ...) fire on shipping-procedure wording ("direct-to-site shipment").
  - The LLM sees only these label summaries for PROCESS, so it flags the section.
- **Fix (agreed, deferred until the new samples arrive so it is tuned on more than 3 SOPs):**
  - Down-weight `reference` for IDs inside procedure sentences; keep full weight for reference units and rows.
  - Split `_SCOPE` into strong and weak cues, so weak cues alone never outweigh procedure or responsibility.
  - Keep the thresholds unchanged.
  - Tests: inline citation → procedure; reference row → reference; "direct-to-site" → not scope; "This SOP applies to all sites" → scope.
  - The goldens and the RPAS split must still hold.

## 2026-10-08: GWP rules extracted by the app (not hand-curated): done, pending human review
- From the user: there are no further SOPs, templates or GWPs; start Phase 8 on the 3 samples. GWP writing rules must be **extracted by the application and passed to the LLM, never hard-coded**. Only preservation rules may be built in (values and document names unchanged). Saved as a memory.
- State before: the sample rule set in `documents/GWP/` was hand-curated by Claude in Phase 4; the LLM extractor had never run on the real guide. Its candidates were also never used: rule selection takes approved rules only.
- **First real extraction** (`gwp_extractor/1`, 2 batches of 16k chars): 12 guide rules, 54 of 119 guide passages neither cited nor skipped, and meaningless checks ("active voice" as a `modality` check, `forbidden_terms: ["idioms", "jargon"]`).
- **Extractor fixes** (`gwp/extractor.py`, `gwp/checks.py`, `gwp/ingest.py`, prompt `gwp_extractor/2`):
  - batches of 6k chars (4 calls on the guide);
  - a **coverage pass**: passages the first pass neither cited nor skipped are sent again, marked `>>`, with their section as context;
  - **grounding checks**: `forbidden_terms` must be quoted in the cited guide text; `callout_palette` colours must appear in it; `modality` and `protected_values` are for PRES rules only. Failing rules are downgraded to `semantic` and listed in the report;
  - callout labels the guide uses ("Key-take-away", "Executive Summary/Introduction") are mapped to callout kinds;
  - **duplicates merged** (same check and params, or at least 60% word overlap with the same category or shared citations), listed as dropped in the report;
  - the guide's own top-level front chapters (Purpose, Applicability, Definitions, Implementation, Roles) are no longer rule input: they describe the guide itself and produced rules like "Include a section for 'Purpose' that outlines the focus on human learning principles".
- **Result** (`BI-VQD-24416-G` v3 in the app's GWP store, status `in_review`): 70 candidate rules (16 STR, 24 STY, 30 FMT) plus the 6 baseline PRES rules; 103/103 rule-input passages cited or skipped; 2 duplicates merged; 3 checks downgraded (e.g. invented colours `#000000`/`#FFFFFF`). Copied to `documents/GWP/BI-VQD-24416-G_v3_rules.json` and `_v3_report.json`.
  - Some near-duplicates remain for the reviewer (e.g. STY-007/STY-008 "focus on the central theme"; FMT-022/FMT-023 "do not overload with infographics").
  - STY-005 reads "two lines" as **20** words (the curated set used 30). STY-019/020 give Flesch at least 30 and grade at most 12. STY-014 forbids "may" (PRES-004 still wins: "may" is flagged, never rewritten).
- **New script** `scripts/extract_gwp.py`: registers a guide in the app's GWP store and runs parse, then LLM extraction, exactly as `POST /api/v1/gwp` + `/extract` (`--approve`, `--copy-to`).
- The hand-curated files are renamed `documents/GWP/BI-VQD-24416-G_curated_{rules,report}.json`; only `tests/test_sample_gwp.py` reads them, as a reference. Superseded extraction versions v1 and v2 were deleted from the store.
- Tests: `test_rule_input_skips_the_guides_own_front_chapters`, `test_duplicate_and_ungrounded_candidates`, coverage pass in `test_extractor_calls_llm_per_batch`.

## 2026-10-08: Phase 8 (slot planner): done
- **Contracts** (`plans.py`, CONTRACTS.md "Slot plans", `examples/slot_plan.json`):
  - `SlotMapping.origin` (rule / llm / human) and `SlotMapping.rule_ids` (the approved STY, PRES and FMT rules the drafter applies, by content type, from the job's rule set);
  - `SectionSlotPlan.region_choices` (`RegionChoice`: a source lead-in that answers an inline choice) and `notes`;
  - `SlotPlan.prompt_version`, `model`, `token_usage`, `gwp_guide_id`, and `SlotPlan.section()`.
- **Signals** (`planning/slot_signals.py`, zero tokens, nothing template-specific):
  - concepts (what, intention, target roles, business units, geography, processes/systems, not covered) are activated by each slot's own key and instruction; their cue words are searched in the passages. A passage feeds its best slot, plus any slot whose concept it states clearly (one sentence naming roles, units and geography feeds all three, each with `extraction_scope`);
  - whole tables by header and cells: abbreviations, terms, role tables, RACI tables (share of R/A/C/I/x cells); a legend table follows the table before it;
  - icon rows: an icon passage plus its short label lines; matched to the section's icon slots **by position** when the counts agree;
  - inline choices: "This SOP is applicable:" answers `p:32` with "SOP";
  - only real data-table rows count as tables: BI-VQD-10505-S keeps its icon rows inside a layout table.
- **Planner** (`planning/slot_planner.py`): rule proposal, then the LLM per block, then the plan.
  - single-content-slot sections take everything; table-only sections place whole tables, with intro lines going to the table they introduce and figures left for the reviewer;
  - rule callouts: source warning → `attention`, note → `explanation`; a fixed callout slot takes the passages promoted to its kind;
  - the LLM sees only sections with doubts or where the template places callout boxes (PROCESS here). It may change slots inside a section, propose callouts (palette kinds only; consecutive text passages; an intro line takes its list), and flag. Invalid items get one repair, then are dropped. It cannot move passages between sections or empty a single-slot section;
  - the GWP's **STR and FMT rules go into the prompt from the job's rule set** (logged as `gwp_rules_in_prompt`); none without a GWP;
  - `migration_action`: `copy_verbatim` in placement mode and for table slots, else `extract_and_rewrite`;
  - empty slots: required → `source_content_not_found`, optional → `not_applicable`, one-of groups as in the template.
- **Budget** (`planning/budget.py`): about 4 chars per token; sections packed into blocks (5000 tokens by default, `slot_planner_block_tokens`); a large section splits at sub-section starts or before a non-list passage, so lists stay with their intro.
- **Validator** (`planning/slot_validator.py`, report scope `slot_plan`): unaccounted passages, passages outside the section plan's scope, out-of-order slots and required slots neither filled nor flagged are gates; gaps, unplaced passages and review marks are listed.
- **Stage, orchestrator, API:** `stages.plan_slots` replaces the stub (settings `slot_planner_llm`, `slot_planner_preview_chars`, `slot_planner_block_tokens`). Slot-plan approval is refused while gate issues are open (`PLAN_HAS_GATE_ISSUES`). `PATCH /slot-plan` returns the validation and marks changed slot mappings `human`; new `GET /slot-plan/validation`.
- **Phase 7 fixes found on the way** (the agreed fix deferred on 2026-10-07, now tuned on the 3 samples since no others are coming):
  - with the app-extracted GWP rules in its prompt, gpt-4o moved 028-BIS-00493's "6.1 GBS Solution: Methodology" (processes and countries in scope of the BPML) out of PROCESS into APPLICABILITY;
  - `signals.py`: scope cues split into strong (applies to, binding for, valid for, not in scope...) and weak (sites, countries, "in scope"...), weak ones counting only next to a strong one; a document ID cited inside a prose sentence is a weak reference signal;
  - `section_planner.py`: under a top-level chapter whose heading names its target, the LLM can no longer move a whole subsection the matcher had no doubt about; the move becomes a reviewer flag. Prompt `section_planner/6` says what APPLICABILITY takes and that GWP structural rules never justify moving a subsection out of its named chapter.
- **Inspection** now runs the slot planner too: a "Slot plan" check, a slot table per section (status, action, passages, GWP rules per slot), callouts and region choices, a "GWP rules" section (which rules went to the prompt), and the golden slot expectations, expected-empty slots and callout suggestions. `--gwp-rules` previews candidate rules as approved and says so.
- **Results on the 3 samples** (all golden checks PASS: section mapping, 34 slot expectations, 5 expected-empty slots):
  - rules only and live (`gpt-4o-poc`, `slot_planner/2`), with and without the GWP; reports in `documents/reports/` (placement mode) and `documents/reports_gwp/` (GWP v3, candidates previewed);
  - BI-VQD-10505-S: 4 icon rows by position, PLW in units, "World-wide" in geography; both "This SOP..." lead-ins → region choice "SOP";
  - LLM callouts match golden suggestions for RPAS ("must not be automated with RPAS" → attention, "organized into 11 steps" → introduction) and, in most runs, BI-VQD-10505-S ("only use the information from the CoC" → attention);
  - cost: 1–2 calls per SOP, about 3.5k–5.3k input and 200–300 output tokens (about 13.5k input tokens for the 3 SOPs).
- **Tests:** `tests/test_v2_slot_planner.py` (23: signals, regions, icon rows, tables, one-of gaps, rule callouts, placement mode vs GWP actions and rule IDs, prompt contents from the job's rule set, LLM apply/validation/repair, single-slot guard, budget splitting, validator gates, stage with and without GWP, API review flow, golden match on samples and a golden negative); `tests/test_v2_slot_planner_live.py` (opt-in `llm`, placement mode and GWP); section-planner tests for the signal fix and the subsection guard. Full suite: 465 passed, 11 skipped (the 2 new live tests are opt-in). Live: 3 passed (use `PYTHONIOENCODING=utf-8` with `-s` on Windows).
- **Open:**
  - The 70 GWP candidate rules need human review before a migration can name the guide (USER_TASKS 3–5).
  - gpt-4o still adds a few review flags of little value (e.g. "may need clarification" on PROCESS passages); they never change placements.
  - Unplaced figures in DEFINITIONS (028-BIS-00493 Image 1) wait for a reviewer decision, as the golden review point says.
  - Callout choices vary between runs (they are suggestions; the reviewer approves them with the slot plan).
- **Next step:** Phase 9 (drafter). Placement mode copies per slot with `extraction_scope`; with a GWP the drafter rewrites using exactly the slot's `rule_ids` from the job's rule set, never built-in style text.

## 2026-10-08: GWP rules approved
- After committing Phase 8 (`ec9a19a`), the user asked to approve the rules. All 70 candidates of `BI-VQD-24416-G` v3 were approved through `service.approve_rules` + `save_reviewed` (as `POST /api/v1/gwp/{guide}/approve` does): 76 rules approved, 0 candidates; the guide is `approved` and the active one. `documents/GWP/BI-VQD-24416-G_v3_rules.json` is refreshed.
- Approved as extracted, so these readings are now in force unless the user changes them: STY-005 at most 20 words per sentence; Flesch at least 30 and grade at most 12; "may" is flagged, never rewritten (PRES-004). The near-duplicates (STY-007/008, FMT-022/023, FMT-003/004/005) are approved too.
- Check: a slot plan for BI-VQD-10505-S with the guide uses `extract_and_rewrite` on text slots and lists 47–49 rule IDs per slot.
- **For Phase 9:** that many rules per slot is too much to paste into every drafter call. The drafter should send only the rules a slot's text can break (STY, plus PRES), group FMT rules for assembly, and merge duplicates at selection time.

## 2026-10-08: Phase 9 (drafter): done
- **Contracts** (`draft.py`, CONTRACTS.md "Drafts", `examples/section_draft.json`): `ClaimKind` (`paragraph`, `bullet`, `step`, `table_row`, `figure`, `caption`, `heading`) on `Claim.kind`; a `heading` claim cites `source_section_id` instead of units.
- **New package `app/services/migration_v2/drafting/`:**
  - `refs.py`: internal cross-references become `{{ref:<source id>}}` tokens before drafting; external document IDs stay as written. `detokenized` puts the source wording back for comparisons, so "described in 6.2" is not reported as a lost number.
  - `memory.py`: bounded memory per call (role names and abbreviations found in the block, and where each reference token points), capped at `drafter_memory_tokens`.
  - `drafter.py`: per slot, by the slot plan's `migration_action`:
    - placement mode (no GWP) and every table slot: passages copied as written. A passage shared by several slots (`extraction_scope`) is cut to its verbatim part by one small LLM call; the answer must be a substring of the passage, else the whole passage is copied with a note;
    - with a GWP: text passages rewritten, one call per block of about 1500 source tokens (`drafter_block_tokens`; at 2500, gpt-4o skipped whole sub-sections). The prompt carries the slots' approved STY and PRES rules from the job's rule set (checkable ones, near-duplicates dropped, at most `drafter_max_rules`), the memory, and the built-in preservation duties. Tables, figures, captions and headings are never sent;
    - **per-passage acceptance**: a claim is dropped if it cites outside its slot or carries a foreign reference token; a passage fails if no kept claim cites it, a reference token is lost, or the Phase 6 comparators find a high or critical change. Valid claims are kept; only the failed passages get a second, targeted call (with the reasons); what fails again is copied verbatim, with one note per slot;
    - deterministic on both paths: source sub-headings as heading claims (in a section's single free-text slot), callout kinds from the slot plan, promoted passages drafted once in place, gap markers for `source_content_not_found`, unplaced passages listed as unresolved.
  - `checks.py`: draft checks, saved as a `quality_report` with scope `drafts`: coverage of every planned passage (`unaccounted_source`), citations inside the slot plan (`unsupported_claim`), reference tokens kept and not invented (`broken_cross_reference`), gap markers where and only where planned (`missing_slot`), the Phase 6 comparators on the detokenized drafts, and the unresolved items.
- **Phase 6 additions found on the way:**
  - `compare_quotes` (PRES-006): a quoted document or system name without an ID must survive. A live rewrite dropped "Refer to 'UiPath Development Guideline'" from RPAS SRC-6-U001 and nothing caught it.
  - `compare_modality`: making a plain statement mandatory ("start with 'BI'" → "must start with") stays allowed, but each case is now a medium review item ("Check that the source means it as a requirement"). gpt-4o does this often under STY-011.
  - Heading claims are skipped by the comparators (they are source headings: "User Account Management" is not an invented role).
- **Prompt** `app/services/llm/prompts/v2/drafter.py`, `drafter/3`: no writing rule in the prompt text; the LLM is asked for `rule_ids_applied` only where a rule changed the wording. Its free-text notes were dropped: gpt-4o filled them with "obligation strength preserved".
- **Stage, API:** `stages.drafting` replaces the stub (settings `drafter_llm`, `drafter_block_tokens`, `drafter_max_rules`, `drafter_memory_tokens`): one `section_draft` artifact per section, the draft report, sections `DRAFTED`, and a `drafter` event (mode, usage, counts). New `GET /drafts`, `GET /drafts/validation`, `GET /sections/{section}/draft`; `PATCH /sections/{section}/slots/{slot}` (was 501) saves a reviewer's claims as a new `human` version once the job has finished drafting, moves it to `MANUALLY_EDITED` and returns the re-run checks.
- **Inspection:** a "Draft" check, a draft table per section (claims, kinds, callouts, citations, GWP rules applied, the source under each rewritten claim, notes), and the golden `subsections_preserved` order and `must_preserve` phrases checked in the draft. In GWP mode a reworded must-preserve phrase is a WARN "check the meaning", not a FAIL.
- **Results on the 3 samples** (live, `gpt-4o-poc`; reports `documents/reports/` placement, `documents/reports_gwp/` GWP v3 approved):
  - every run: unit coverage 100%, no blocking draft issue, golden sub-headings in order;
  - placement mode: golden PASS including all must-preserve phrases; 120/159/82 claims; only 028-BIS-00493 needed an LLM call (one excerpt call, about 480 tokens);
  - GWP mode: 43–95 claims per SOP carry rules; 2–10 passages per SOP copied verbatim after gpt-4o changed an obligation twice ("may" → "must", "must" dropped); 2 plain → mandatory review items on BI-VQD-10505-S; must-preserve phrases reworded (listed for a meaning check);
  - cost with a GWP: 4–8 calls per SOP, about 9k–18k input and 4k–9k output tokens (about 41k input and 19k output for the 3 SOPs).
- **Tests:** `tests/test_v2_drafter.py` (12: tokens and read-back, memory cap, placement structure without an LLM, verbatim excerpts, rewrite prompt from the job's rules, targeted retry and verbatim fallback, a failed call and a dropped quoted title, draft-check gates, stage in placement and GWP mode, API read/edit, samples in placement mode against the goldens); `tests/test_v2_drafter_live.py` (opt-in `llm`, both modes, passed); Phase 6 tests for the quote check and the review item. Full suite: 479 passed, 13 skipped.
- **Found, not fixed:** 028-BIS-00535 (RPAS) itself contains U+FFFD replacement characters where quotes and apostrophes were ("RPAS�s", "�BI�"); they are in the DOCX's XML, not an extraction bug. The migrated document would carry them; Phase 11 or the author should fix the source.
- **Open:**
  - "Summary slots" derived from procedure claims: this template has none (PROCESS has one content slot), so nothing derives yet.
  - Semantic omissions without a value, ID, quote or modality (a dropped clause) are not caught deterministically; the Phase 10 critic is for that.
  - gpt-4o's `rule_ids_applied` is generous; Phase 10's deterministic checks (`max_sentence_words`, `forbidden_terms`, readability) will show which rules a draft really meets.
- **Next step:** Phase 10 (validation, critic, repair, gates) over the `drafts` report, or Phase 11 (Word renderer) to see a document. The plan's order is Phase 10.

## 2026-10-08: Phase 10 (validation, critic, repair, gates): done
- Phase 9 was not committed at the start; Phases 9 and 10 are both in the working tree.
- **Contracts** (`quality.py`, CONTRACTS.md "Quality reports", `examples/quality_report.json`): `RiskTag`; `QualityReport.high_risk_units` and `gate_counts`.
- **New modules in `app/services/migration_v2/quality/`:**
  - `validator.py`: the Phase 9 draft checks, plus:
    - structure: a drafted slot must belong to its template section and have an anchor;
    - order: claims follow source order; a procedure out of order is `sequence_violation`;
    - placeholders: `[TBD]` and similar are high when the source does not have them, medium when it does;
    - callouts: a claim's kind must come from its slot or the slot plan's assignments, and be in the palette.
  - `style.py`: the job's deterministic STY rules (`max_sentence_words`, `forbidden_terms`, `passive_ratio`, `readability`) on the slots the slot plan rewrites. Findings are low issues and soft scores only.
    - A modal word the cited source also states is not a violation, because PRES-004 wins.
    - Syllables are estimated in code; no `textstat` dependency.
  - `risk.py`: a deterministic high-risk classifier: acceptance criteria, safety, regulatory commitment, approval, deadline, numeric limit, retention, escalation, prohibition. It uses cue phrases plus the protected facts.
    - A bare "GxP" or "GMP" is a domain label, not a commitment. It had tagged 28 passages of 028-BIS-00493 as regulatory.
  - `critic.py` + prompt `critic/1`: one call per section (split at `critic_block_tokens`).
    - It reads only reworded claims, each next to its tokenized sources, with the job's PRES rules.
    - Problem kinds: meaning_changed, omission, condition_lost, unsupported_addition, contradiction, source_conflict, wrong_slot, ambiguity, redundancy.
    - Findings name only claims it was shown. Ambiguity and redundancy are capped at medium.
    - It never sets a gate. A call that fails twice leaves a low `[critic:unavailable]` note.
  - `repair.py`: re-drafts only the passages an issue points at.
    - `Drafter.restore` loads the saved draft into a run. The claims citing affected passages are dropped, those passages are copied or rewritten once with the issues as reasons (the drafter's per-passage checks apply), and the section is rebuilt: source order, headings, gap markers and callouts derived again.
    - Order and callout issues need only the rebuild.
    - Never repaired: `missing_slot`, `missing_section`, the critic's `source_conflict` and `wrong_slot`, and sections a reviewer edited.
  - `gates.py`: the job's quality report.
    - Gaps: one `missing_slot` issue per gap marker; resolving it accepts the slot as N/A.
    - Missing required sections.
    - High-risk escalation: an open medium+ deterministic finding, or a high critic finding, on a high-risk unit gets the `high_risk_unresolved` gate.
    - One medium "reworded high-risk content" item per slot.
    - Resolutions carry over by issue ID; `gate_counts` lists open issues per gate.
    - `final_status`: open gate or high issue → HUMAN_REVIEW_REQUIRED; open medium issue or no DOCX yet → COMPLETED_WITH_WARNINGS; else COMPLETED.
- **Stages:** `validating`, `repairing` and `quality_review` replace the stubs. Settings: `critic_llm`, `critic_block_tokens`, `repair_max_attempts` (2).
  - VALIDATING writes a `quality_report` with scope `validation` per round. A section with an open repairable issue goes to REPAIRING while it has attempts left. A blocking issue only a reviewer can settle marks it `needs_review`.
  - Section statuses: `drafted` → `repairing` → `drafted` → `validated` / `needs_review`; `attempts` counts repair rounds.
  - Only ASSEMBLING and RECONCILING are still stubs. With no DOCX yet, a clean job ends COMPLETED_WITH_WARNINGS.
- **Two design choices made on the live samples (recorded in IMPLEMENTATION_PLAN.md, Phase 10 "As built"):**
  - **The critic reads each claim once.** After a repair it reads only the claims whose text changed; earlier findings move to the renumbered IDs of unchanged claims. Re-reading the whole section let gpt-4o flag different claims in round 3 (RPAS: C-TGT-6-117/123/137), after the last repair.
  - **The last repair attempt copies from the source.** The first repair re-words with the reasons; the last copies the flagged passage verbatim, with a note naming the finding. Before this, 14 critic findings were still open on 028-BIS-00493 after 2 rounds, because each LLM repair produced new wording to flag. Meaning wins over house style (§2.1).
- **Orchestrator / API:**
  - `MANUALLY_EDITED` can be retried, e.g. from VALIDATING after a reviewer's edit; a slot edit marks its section `drafted`, so the critic reads it again.
  - New `POST /{job_id}/issues/{issue_id}/resolve` (note required): resolves one issue as a new report version. When the last blocking issue of a HUMAN_REVIEW_REQUIRED job is resolved, the job moves on.
  - `GET /validation` now returns the job's quality report.
  - Note: `POST /retry` takes `from_stage` as a **query parameter**. Without it a retry restarts at PARSING and re-drafts everything, including a reviewer's edits.
- **Inspection:** runs the validation and repair rounds and `quality_review`, as the orchestrator does.
  - New "Quality gates" check: gaps and high-risk findings are WARN (reviewer); any other open gate is FAIL.
  - New "Quality" section: the gate table, rounds, open issues, soft scores and high-risk passages.
  - The LLM check now also lists the critic and repair usage.
- **Results on the 3 samples** (live, `gpt-4o-poc`; reports refreshed in `documents/reports/` and `documents/reports_gwp/`):
  - Placement mode: 1 validation round, 0 critic calls, 0 repairs.
    - 028-BIS-00493 and BI-VQD-10505-S: no open gate (BI-VQD-10505-S Quality gates PASS).
    - RPAS: the 2 required APPLICABILITY gaps (units, geography) wait for a reviewer.
  - GWP mode (v3 approved): 3 validation rounds and 2 repair rounds per SOP.
    - 028-BIS-00493 and BI-VQD-10505-S: no open gate.
    - RPAS: its 2 gaps, plus 1 high-risk item ("states no obligation; the draft makes it mandatory" on an approval passage).
    - The critic found real problems the deterministic checks miss: "might be skipped" → "may be omitted"; an added "Refer to the united System SOP"; a dropped "machine learning and AI bots are excluded".
    - Cost per SOP: critic 6–7 calls, about 9k–13k input and 400–900 output tokens; repair 1 call, about 1.5k input.
  - Soft scores with the GWP: style compliance 0.40–0.64, passive 33–51%, Flesch 30–38, grade 11.7–13.8. gpt-4o's rewrites meet the guide's 20-word sentences and 10% passive only partly; these are low items, not gates.
- **Tests:**
  - `tests/test_v2_quality.py` (16 tests):
    - the seeded faults (dropped number, weakened prohibition, reordered procedure) are caught, then repaired;
    - a repair that breaks the text again falls back to the source; repair without an LLM; gaps are never repaired;
    - callouts, placeholders, unknown slots; style checks (soft, rewritten slots only, the PRES-004 exception);
    - readability helpers; the risk classifier; critic scoping, capping and failure;
    - gates: gaps, resolutions, final status, missing section; high-risk escalation;
    - the stage loop: last attempt verbatim, one repair clears a finding, a source conflict goes to the reviewer;
    - the API: resolve a gap, retry keeps the resolution, an edit is re-validated and not repaired;
    - the samples in placement mode need no repair.
  - `tests/test_v2_quality_live.py` (opt-in `llm`, passed).
  - Older tests now expect the example job to end HUMAN_REVIEW_REQUIRED, because its required `steps` slot has no source content (`END_STATUS`); `/validation` is no longer a 404.
  - Full suite: 495 passed, 14 skipped.
- **Open:**
  - **RPAS gaps** (APPLICABILITY units, geography) and the high-risk "made mandatory" item need a reviewer in a real job (resolve via the API, or the review UI in Phase 13).
  - A reviewer who fills a gap slot must cite a source unit; one planned for another slot gives an `unsupported_claim`. The Phase 13 UI needs a way to add reviewer content to a gap.
  - The critic is generous with medium "meaning_changed" findings on near-synonyms ("dissemination" vs "distribution"). They stay medium items (COMPLETED_WITH_WARNINGS) and are never repaired.
  - The style scores are far from the guide's targets. Whether the drafter prompt should push harder on STY-005 and STY-021 is a decision for the user.
  - Calibration of the risk cues and critic severities waits for Phase 14 (approved examples).
- **Next step:** Phase 11 (anchor-based Word renderer v2), starting from the `SectionDraft`s and the template anchors. Phase 12 then resolves the `{{ref:...}}` tokens and removes accepted gaps on export.

## 2026-10-09: Phase 11 (anchor-based Word renderer): done
- Phases 9 and 10 were still uncommitted at the start; Phases 9–11 are all in the working tree.
- **New package `app/services/migration_v2/render/`:**
  - `renderer.py`: `render_document(template, drafts, source, out, mode=review|final, accepted_gaps, slot_plan, ref_text)` fills a copy of the template's normalized file in place, at each slot's anchor (content control, row controls of a table slot, table cell, bookmark, placeholder).
    - Content → filled.
    - Required slot without source content → gap marker "Source content not found. Human review required." (review draft).
    - Optional slot without content → removed with its instruction (a cell's row, a callout box, a data table, a block).
    - Optional sections without content are removed. The highlighted "(optional)" leaves a heading whose section has content. The callout legend table goes.
    - `final` mode removes reviewer-accepted gaps and unwraps the slot content controls. It raises `RenderBlocked` while any gap is unresolved. Phase 12 wires it to the export.
  - `content.py`: claims → blocks.
    - Paragraphs keep the anchor's alignment and the template's spacing; multi-line claims get line breaks.
    - Steps and bullets use real Word numbering. Sub-headings use `Heading n` with the template headings' numbering (6.1, 6.2.1).
    - Figures come from the source image, scaled to the text width (a placeholder and a warning if the file is missing). Captions use the `Caption` style.
    - Adjacent claims with a callout kind go into one cloned callout box.
    - Reference tokens show the source's wording until Phase 12, without repeating words the text already has before the token.
  - `numbering.py`: bullets reuse the template's bullet definition. Numbered lists use a decimal definition built from the bullet definition's indents (the GP template has no decimal list), with one `w:num` per list that restarts at 1.
  - `tables.py`:
    - A table slot fills the template table. The header rows stay; each source row is a copy of the first example row, with cell shading from `fill_hex`; the example rows go.
    - A source table with more columns than the template, or a template header with placeholders ("[Role 1]"), is written in its own columns. It takes the template table's formatting, widths from its content, and rows that may break across pages.
    - A header row that only repeats the rows (a source table whose every row was marked as a header) is dropped.
    - Each further source table in a slot gets its own table. Claim order is kept, so a lead-in line stays before its table. No cell is dropped.
  - `callouts.py`: callout boxes are deep copies of the palette's prototype tables (fill, icon, widths), kept on one page. Without a prototype, a box is built from the `CalloutStyle`.
  - `instructions.py`:
    - Conditional regions are settled first. An inline choice takes the slot plan's choice, else the document type another region of the same SOP settled, else it is removed with a note. A block region is kept (made plain) only when the slot plan has a `RegionChoice` for it.
    - Then the remaining blue instruction text is removed: blue paragraphs, blue spacer lines, all-blue tables, and blue runs inside black paragraphs.
    - Headings, the TOC and rendered content are never touched.
  - `verify.py`, the post-render check (`RenderReport.problems`):
    - every claim of a filled slot is in the document (table rows cell by cell, figures by their image's hash);
    - gap markers match the gap slots;
    - no blue text is left, and no `COND_` controls (no `CC_` controls in final mode);
    - numbering IDs exist and drawing IDs are unique;
    - headers and footers are byte-identical to the template's.
  - Word refreshes the TOC on opening (`w:updateFields`). Drawing IDs are renumbered. A replaced bookmark paragraph's bookmark moves to the new content.
- **Shared colour classifier** `template/colour.py` (`TextColour`), moved out of `slot_detector.py`, so detection and removal agree on what is blue. Detection behaves as before.
- **Contracts** (CONTRACTS.md "Rendered document", `examples/render_report.json`):
  - `render.py`: `RenderReport`, `SlotRender`, `RegionRender`, `RenderMode`, `SlotOutcome`, `RegionOutcome`;
  - `ArtifactKind.RENDER_REPORT`;
  - `TableCell.paragraphs`: the Phase 2 exporter now lists a cell's paragraphs when it has several. `text` is unchanged (still joined with spaces), so hashes, checks and goldens are unaffected; the renderer uses `paragraphs` to keep the cell's lines (document history, roles tables).
- **Stage, API:**
  - ASSEMBLING (was a stub) renders the review draft. It writes a `docx` artifact (`ArtifactWriter.write_file` / `read_bytes`, sha256 as for JSON), a `render_report` and a `render` event.
  - A missing template file is a `render_failed` event; the job then ends without a document.
  - QUALITY_REVIEW and issue resolution count a document only when its render report has no problems (`stages.document_rendered`).
  - New `GET /{id}/document` (the `.docx`, `?version=`) and `GET /{id}/document/report`. `GET /artifacts/docx` returns the file.
- **Inspection:** runs ASSEMBLING as the orchestrator does.
  - New "Word document" check: FAIL on a post-render problem or a slot without an anchor; WARN with the warnings.
  - New "Word document" section: a link to `docx_v1.docx`, the slot outcomes, the conditional regions and the warnings.
- **Results on the 3 samples** (live `gpt-4o-poc`, placement mode and GWP v3; reports refreshed in `documents/reports/` and `documents/reports_gwp/`):
  - Every document opens in Word without repair. No post-render problem; no slot without an anchor.
  - BI-VQD-10505-S has 10–11 pages, 028-BIS-00493 22, RPAS 21. Icons, header, footer and cover page are kept. The TOC refreshes with the new sub-headings. Word numbers 6.1 and 6.2.1. Figures and captions are in place.
  - "This SOP:" and "This SOP is applicable:" are applied on BI-VQD-10505-S. They are removed on the two SOPs whose source has no such line (028-BIS-00493, RPAS).
  - RPAS has 2 gap markers (APPLICABILITY units, geography). In `final` mode with both accepted, their icon rows are gone.
  - Written in their own columns: the DOCUMENT HISTORY tables of 028-BIS-00493 and RPAS (4 columns including "Expert team"; the template has 3), and both RPAS RACI tables (the template header has "[Role n]" placeholders).
  - Column warnings for the reviewer: RPAS "Role assigned to:" under the template's "Competence"; "Document-ID" under "Name"; "Definition" under "Description".
- **Tests:**
  - `tests/test_v2_renderer.py` (19):
    - content at each anchor kind (control, row controls, table cell, bookmark, placeholder);
    - real numbering (one list across a nested bullet); sub-heading, figure and caption; a missing figure file;
    - the callout clone (fill, icon, kept on one page);
    - the template table filled with shading; a table in its own columns; claim order; cell lines;
    - a gap in review mode, final mode blocked, final mode with an accepted gap; an optional slot removed;
    - conditional regions (choice, kept, removed);
    - the post-render check catching a missing claim and a missing figure; reference wording;
    - the ASSEMBLING stage (artifacts, `render_failed`) and the API endpoints;
    - the 3 samples in placement mode.
  - `tests/test_word_roundtrip.py`: a rendered document opens in Word (opt-in `word`, passed).
  - A contract test for `render_report.json`.
  - Full suite: 516 passed, 15 skipped.
- **Open:**
  - **For the user:** an inline choice ("This Directive/SOP/Work Instruction/Guidance:") with no answering line in the SOP is removed. Should it default to the SOP's document type ("SOP")?
  - **For the user:** the competence table (t:7) is removed unless the slot plan keeps it, and no rule keeps it yet. The golden for BI-VQD-10505-S expects the Competence column to be empty, never invented.
  - **For the user:** a source table with more columns than the template table (a document history with "Expert team", RACI) is written in its own columns. The alternative is to fit it into the template's columns, which would lose or merge a column.
  - Bullets inside source table cells come through as plain lines (`TableCell.paragraphs` has no list information).
  - Word asks "update fields?" when it opens the review draft (`updateFields`). A viewer that does not update fields shows the template's old TOC until the file is opened in Word. Phase 12 can write the TOC entries itself if that matters.
  - Rows of a filled template table are not wrapped in content controls in the review draft (block and cell controls are kept).
  - RPAS: the U+FFFD characters in the source file carry into the document; the render report warns about them.
- **Next step:** Phase 12.
  - Build the `NumberMap` from the assembled drafts.
  - Resolve `{{ref:...}}` tokens to Word `REF` fields on bookmarks (the renderer's `ref_text` hook).
  - Reconcile.
  - Then the final export at `GET /{id}/export/word`: `render_document(mode=final, accepted_gaps=<resolved missing_slot issues>)`, refused while a gap is open (`RenderBlocked`).

## 2026-10-10: Fixes from the user's review of the Word output: TOC, shared APPLICABILITY passage: done
- **Problem 1: the table of contents was the template's.** The renderer left the template's cached TOC entries (its own sections, including the removed "distribution of controlled prints", "TEMPLATE DOCUMENT HISTORY", no sub-headings, the template's page numbers) and relied on `w:updateFields`. Word shows the right TOC only after answering Yes to "Update the fields?"; any other viewer, or a No, shows the template's list.
  - New `render/toc.py`: `rebuild_toc` writes the entries from the rendered headings. The levels come from the field's `\t` or `\o` switch (GP template: Heading 1–3). Each entry has the Word number (`NumberingResolver`), the text, a hyperlink and a `PAGEREF` to a bookmark on the heading (the heading's own bookmark, else a new `_Toc…` one). The TOC field stays, so Word's "Update table" still works.
  - New `render/pages.py` + `scripts/word_pages.ps1`: page numbers measured by Word's layout. Word opens the rendered file read-only and hidden, reads each bookmark's page and closes without saving, so the XML stays the renderer's (headers stay byte-identical). Then `updateFields` is dropped: no prompt on opening. Setting `render_toc_pages` (`word` default, `off`); without Word, the entries have no page numbers and `updateFields` stays.
  - 028-BIS-00493: 17 entries (1–9 with 6.1, 6.1.1, 6.1.2, 6.2, 6.2.1–6.2.3, 6.3). The cached page numbers are identical to Word's own `TablesOfContents(1).Update()` on the same file.
  - The post-render check (`verify.py`) now fails when the TOC does not match the headings or links to a missing bookmark. `RenderReport.toc_entries` and `toc_page_numbers`.
- **Problem 2: one APPLICABILITY sentence copied whole into roles, units and geography (028-BIS-00493).** The slot planner shares SRC-2-U001 between the 3 slots (`extraction_scope`), as the golden expects. But the placement-mode excerpt call accepted any substring, and gpt-4o returned the whole sentence for each slot. In GWP mode the rewrite only got a "(part: X)" hint and rewrote the whole sentence 3 times.
  - The drafter now **splits** a shared text passage (both modes; prompt `drafter/4`). One LLM call per passage cuts it into consecutive verbatim pieces, each given to one of its slots. `split_passage` accepts the split only if the pieces, in order, are the whole passage: nothing dropped, added or repeated, only spaces and punctuation between pieces, and every slot gets a piece. Each slot then copies (placement) or rewrites (GWP) only its part.
  - The part is kept on the claims as `Claim.spans` (`EvidenceSpan`). `restore` uses them, so a repair re-drafts the slot's part, not the passage. The draft checks expect a reference token only in the slot whose part has it. The critic is shown the part, not the whole passage.
  - When a passage cannot be split cleanly (or there is no LLM), each slot gets it whole, with one note for the reviewer (as before).
  - 028-BIS-00493 live (gpt-4o-poc), placement: roles "This procedure is binding for BI employees, including temporary employees and contractors" | units "working in the GBS unITed Program, for the GBS Deployments and for the GBS Live Site Organization" | geography "independent of the organizational assignment to a country, site or division." GWP: each part rewritten on its own. No gate opens in either mode; the golden comparison is unchanged.
- **Tests:** `test_v2_drafter.py` (the excerpt test is replaced: `split_passage` rules, a clean split, a refused split, GWP rewrite of the parts, `restore` keeps the part). `test_v2_renderer.py` (TOC levels, entries = headings with bookmarks, page counter, no Word, stale TOC caught; the 3 samples' TOCs). `tests/conftest.py` turns the Word page count off unless `SOP_RUN_WORD_TESTS=1`. Full suite: 524 passed, 15 skipped.
- **Open:** the reports in `documents/reports*/` are from 2026-10-09. Re-run `scripts/inspect_sop.py` to refresh them with these fixes. `reports_gwp/028-BIS-00493/docx_v1.docx` was open in Word, so it was not overwritten.

## 2026-10-10: Figures and narrative in a table-only section (DEFINITIONS): done
- **Problem:** Image 1 and the paragraph before it ("The GBS Solution provided by the GBS unITed Program operates cross functional…") were missing from 028-BIS-00493's DEFINITIONS. The section's template slots are both tables. The rules left the figure and caption in no slot ([CHECK]), and put the long paragraph beside the terms table ([CHECK]). The slot planner's LLM then answered "fits no slot" ("figures and captions should not be placed in slots"; the answer varied between runs). A passage in no slot was only a medium note, so the job completed and nothing was in the document. The golden file's `figures` expectations were never checked.
- **User decision:** keep such content in DEFINITIONS, below the tables.
- **Done:**
  - Slot planner: in a table-only section, a run of passages holding a figure, a caption or long text (more than 300 chars, not a table intro) goes into the section's last table slot as `SlotMapping.below_unit_ids`. A line that introduces the next table stays with it. These lines show `[BELOW TABLES]` to the LLM and cannot be changed. The LLM can no longer take a passage the rules placed out of every slot (prompt `slot_planner/3`).
  - Drafter: below passages sort after the rows (`Drafter.position`, also in `restore` for repairs). The validator's order check knows about them. The renderer needs no change: text claims after the rows are written after the table.
  - Gates: each passage still in no slot is a high `unaccounted_source` issue (`[unplaced]`, `gates.unplaced_issues`). It waits for the reviewer like a gap: HUMAN_REVIEW_REQUIRED until resolved.
  - Inspection: the golden `figures` are now checked (`draft_figure_problems`: the caption claim and its figure in the draft).
- **Result** (028-BIS-00493, live gpt-4o-poc, both modes): no unplaced passage. After the abbreviations table come the paragraph, the image and "Image 1: Dimensions of the GBS unITed Program", before chapter 4. No gate open. Placement mode: Slot plan, Draft and Quality gates are now PASS (were WARN).
- **Tests:** slot planner (below-the-tables placement, an intro line stays, the LLM cannot drop or move them); drafter (rows first, order check, repair rebuild); gates (an unplaced passage blocks, awaits the reviewer); golden figures; sample render (Image 1 after the abbreviations table). Full suite: 529 passed, 15 skipped.

## 2026-10-10: Phase 12 (assembly, cross-references, reconciliation, audit, export): done
- **Order of operations** (new status `RENDERING`): VALIDATING (⇄ REPAIRING) → ASSEMBLING → RECONCILING → RENDERING → QUALITY_REVIEW.
- **Assembly** (`app/services/migration_v2/assembly/`):
  - `numbers.py` builds the `NumberMap` exactly as the renderer will number things. Chapters present: an optional chapter with no content is removed and the ones after it move up. Sub-headings: chapter number plus `list_level` depth, a skipped level counting from 1 as in Word. Referenced passages: their holder's number, plus the "No." cell or step number.
  - A source section with no heading of its own (merged, or split) gets its holder's number, marked `exact: false`.
  - `xref_resolver.py` keeps the source's phrase and replaces only its numbers ("chapter 6.12.1" → "chapter 6.13.1", "Chapter 8, no. 17" keeps 17). Status `resolved` / `merged` (medium issue) / `unresolved` (high `broken_cross_reference`). It also checks relative phrases ("described above") against the passage just before or after in the source.
  - `AssembledDocument` and `NumberMap` artifacts; RENDERING and RECONCILING reuse them while the drafts are unchanged, and make them again after an edit or a patch.
- **Renderer:** chapter and sub-heading bookmarks (`_Ref_<id>`); each new number is a Word `REF \w \h` field whose cached value is the computed number (`xml.ref_field`, `add_runs`).
  - The post-render check compares Word's numbering of every bookmarked heading (`NumberingResolver`) with the map, and finds REF fields without a bookmark.
  - A template whose headings Word does not number gets plain numbers and a warning (`headings_numbered`).
  - On the 3 samples, Word's own field update gives the same values as the cached ones (checked through COM: 8/7/6.1.2.1; RPAS 5, 6.13.1, 6.9, 6.5; BI-VQD-10505-S 6.2).
- **Reconciliation** (`quality/reconcile.py`, prompt `reconcile/1`):
  - Deterministic checks: abbreviations (not defined, spelled out late, spelled out differently from the table; one summary item for the undefined ones, and only when the document has an abbreviations table); roles not in the roles table (one summary item); duplicate heading numbers (gate).
  - One LLM call, by default for GWP jobs (`reconcile_llm: gwp`), reports contradictions, duplicates and terms named two ways. A patch is applied only to a reworded claim (judged as the critic does, not by `rule_ids_applied`), only with the same reference tokens, and only if the whole document still validates with no new blocking issue on that claim. Patched sections are a new draft version with `origin: reconcile`.
  - RECONCILING writes the next `validation` round (validation, carried critic findings, reference and reconciliation issues).
- **Audit** (`audit.py`): the orchestrator and the inspection runner wrap the chain factory per stage. Every LLM call (and every failed attempt) is an `llm_call` event with task, prompt version, model, tokens, latency, retry, status, and the unit and claim IDs supplied. Reviewer edits: `slot_edited` (claim-by-claim before/after) and `plan_edited`, with the reviewer as actor.
- **Export** (`export.py`, API):
  - `GET /{id}/export/word` is refused (409 `EXPORT_BLOCKED`, `GAPS_UNRESOLVED`, `EXPORT_CHECK_FAILED`, `RENDER_FAILED`) until the job completed. It renders `final` with accepted gaps removed (re-assembled: numbers can change), saves the final artifacts (`scope: final`) and `traceability`, writes `gap_removed` events and an `export` event, and marks the SOP record (`migrated_path`, `migrated_job_id`, `migrated_at`). Asking again with nothing changed returns the same file.
  - `GET /{id}/traceability?format=json|csv` builds the mapping live from the latest drafts.
- **Inspection:** runs the new stages, audited. New "Cross-references" check and "Numbers and cross-references" section. The LLM check now comes from the `llm_call` events. The golden comparison checks `figures` and `cross_references` (the reference lands on the expected heading and entry).
- **End to end** (the phase's done-check, live gpt-4o-poc, real orchestrator, auto mode):
  - 028-BIS-00493 with GWP v3: COMPLETED_WITH_WARNINGS. 16/16 references resolved, 1 reconciliation patch applied (then validated and re-assembled), final export written with Word-measured TOC pages. 17 LLM calls, each with every audit field.
  - RPAS: HUMAN_REVIEW_REQUIRED (the two APPLICABILITY gaps, plus "Chapter 5.2" → "Chapter 5" escalated as high-risk). The export is refused with those three reasons.
- **Tests:** `tests/test_v2_assembly.py` (19): numbers, references, show(), merged/unresolved, relative, REF runs, unnumbered templates, reconciliation checks and patches, audit, export refusal/success/reuse, traceability, the edit diff, the reconciling stage with a patch.
  - Updated: job stage sequence (`RENDERING`); the example SOP's dangling "see Section 5.1" is now a gate the tests resolve; the renderer stage test; the sample render test checks the REF fields.
  - `tests/conftest.py`: no Word unless `SOP_RUN_WORD_TESTS=1`.
  - Full suite: 547 passed, 15 skipped.
- **Open:**
  - The two decisions in USER_TASKS 5j.
  - The example template has no `.docx`, so the API export test covers refusal and edits; the success path is tested on the service with the synthetic template.
  - `documents/reports*/` are from before Phase 12: re-run `scripts/inspect_sop.py` to refresh them.
- **Next step:** Phase 13 (review UI), against the endpoints above, or Phase 14 (evaluation harness).

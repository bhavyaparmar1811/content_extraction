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

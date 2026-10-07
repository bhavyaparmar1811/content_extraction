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
| `quality.py` | `ProtectedFacts`, `ValidationIssue`, `QualityReport`, `Gate`, `Modality` | [`quality_report.json`](examples/quality_report.json) |
| `job.py` | `MigrationJob`, `JobStatus`, `SectionState`, `ArtifactRef`, `ArtifactKind` | [`migration_job.json`](examples/migration_job.json) |
| `refs.py` | `CrossReference`, `NumberMap`, `make_ref_token()`, `find_ref_tokens()` | [`number_map.json`](examples/number_map.json) |
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
- **Job artifacts** are immutable files `data/migrations/{job_id}/{kind}[_{scope}]_v{n}.json`, never overwritten. `ArtifactRef.sha256` is checked on every read. A plan's `version` equals its artifact version. Approval writes a new version with `approved_by` set. PARSING snapshots the source model, template model and GWP rules, so later edits to the SOP, template or guide never change a running job.
- **Callouts.** The template's colour-coded boxes form `TemplateModel.callout_palette`, one `CalloutStyle` per `CalloutKind` (`introduction`, `explanation`, `attention`, `key_takeaway`) with the template's exact fill, icon and layout.
  - Content gets a kind in two ways. A **fixed callout slot** (`TargetSlot.callout_kind`) is a box placed in the template. A **promotion** (`SectionSlotPlan.callout_assignments`) wraps source units, by a rule (warning → attention, note → explanation), the LLM or a human, and is reviewed with the slot plan.
  - Claims carry the kind as `Claim.callout_kind`. The renderer clones the palette's prototype table, so colours never pass through the LLM.
  - Meaningful source cell colours are kept as `TableCell.fill_hex`; plain header fills are dropped.
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
- **Language.** `SourceDocument.language` is the hook for translation later; claims are the unit of translation.

## Changing a contract

1. Change the model in `app/schemas/v2/`.
2. Update the matching example in `examples/`.
3. Run `pytest tests/test_v2_contracts.py`.
4. Note the change in `PROGRESS.md`, because later phases depend on these shapes.

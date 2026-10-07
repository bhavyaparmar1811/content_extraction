# Migration v2 sample inputs

Real input files used to build and evaluate the v2 migration pipeline (see `docs/migration_v2/IMPLEMENTATION_PLAN.md`).

| Folder | Contents |
|---|---|
| `sop/` | Legacy source SOP/GP documents (`.docx` or `.pdf`) |
| `template/` | Target Word templates (`.docx`) |
| `gwp/` | Good Writing Practice guide (`.docx` or `.pdf`) |
| `golden/` | Hand-written expected outcomes, one JSON per SOP (format below) |

**Confidential files must not go here.** Put them in the gitignored `documents/` folder at the project root (`SOPs/`, `Templates/`, `GWP/`, `golden/`), or in `data/samples/` (same sub-folders as here). Tests look in all three locations and skip when a file is absent.

## Provenance

| File | Source | Version | Committable? |
|---|---|---|---|
| `documents/SOPs/028-BIS-00493.docx` | BI internal SOP | 3.0 | No |
| `documents/SOPs/028-BIS-00535_1.0_...(RPAS).docx` | BI internal SOP | 1.0 | No |
| `documents/SOPs/BI-VQD-10505-S.docx` | BI internal SOP | 5.0 | No |
| `documents/Templates/Template Main GP Docs.docx` | BI GP Docs main template | n/a | No |
| `documents/golden/*.json` | Drafted from the SOPs above, pending SME review | n/a | No (quotes SOP text) |

## Golden expectation format (`golden/<sop_file_stem>.json`)

Partial expectations are fine. Fill in what you are sure of. `target_slot` is a `ContentType` value until template slot IDs exist.

Optional fields used by the drafted goldens:
- `mapping_type` and `split_note` on a section mapping (`one_to_one`, `split`, `unresolved`, …);
- `subsections_preserved` or `step_order`: sub-headings that must survive in this order;
- `boilerplate`: source regions that must not become migrated text (cover sheet, TOC);
- `callout_candidates`: `{source_text_contains, kind, confidence}`, where `suggested` means a reviewer may reasonably disagree;
- `review_points`: known judgement calls that the evaluation should flag, not score.
- `target_slot_ref` on a slot expectation: the exact template slot, written as `<SECTION>.<key>`. The icon rows of the main GP template are:

  | Ref | Template instruction (icon row) |
  |---|---|
  | `PURPOSE.what` | Brief description of what the document is about / what process is described |
  | `PURPOSE.intention` | Brief description of the intention / what you want to achieve |
  | `APPLICABILITY.roles` | Which target roles must follow this document |
  | `APPLICABILITY.units` | Business units, group functions, departments |
  | `APPLICABILITY.geography` | World-wide, R/OPU, country or site |
  | `APPLICABILITY.processes` | Affected processes, systems or materials |
  | `APPLICABILITY.not_covered` | Paragraph below the table, no icon: topics not covered |

  Icons are matched by row position and instruction, never by image. Source icon files differ from the template's.
- Slot refs resolve with `TemplateModel.slot_by_ref`; `tests/test_sample_template_model.py` checks that every ref in the goldens exists in the detected template.
- The sample template's reviewed config (region decisions) is `documents/template_config/Template_Main_GP_Docs.json`, also confidential.
- `expected_empty_slots`: `{slot_ref, reason}` for slots the source can't fill. They should become gap markers or be removed, never invented.
- `figures`: `{caption_contains, section}` for diagrams that must stay images with their captions, and never become icons.
- `source_icon_rows`: icon rows in the source, with the template slots each one feeds.

```json
{
  "sop_file": "SOP-QA-001.docx",
  "template_file": "Template_Main_GP_Docs.docx",
  "section_mapping": [
    {"source_heading": "4.2 Deviation Processing", "target_heading": "3. Performing the Process"}
  ],
  "must_preserve": [
    "within five business days",
    "QA must approve the deviation before closure"
  ],
  "slot_expectations": [
    {"source_text_contains": "submits the deviation within five business days", "target_slot": "timing"}
  ],
  "cross_references": [
    {"source_text": "see Section 4.2", "expected_target_heading": "3. Performing the Process"}
  ]
}
```

# Final GP Document Migration Architecture and Workflow

## 1. Purpose

This document defines the final architecture for migrating a legacy source SOP or GP document into a structured target Word template. The architecture is designed to:

- Preserve source meaning, requirements, facts, sequence, and traceability.
- Map source content into the correct target sections.
- Map content within each target section to semantic instruction slots.
- Support instructions both with and without icons.
- Rewrite approved source content according to Good Writing Practices (GWP).
- Avoid unbounded LLM context growth.
- Prevent unsupported content, accidental omissions, and changed obligations.
- Preserve the target Word template's layout, icons, instructions, styles, and insertion locations.
- Support human review at section-plan, slot-plan, and quality-review stages.

The fundamental strategy is hierarchical:

```text
Level 1: Source section(s) -> Target section
Level 2: Ordered source units -> Semantic instruction slots inside that target section
Level 3: Approved source evidence -> GWP-compliant slot content
Level 4: Validated slot content -> Exact Word insertion anchor
```

An icon is optional. A target slot is defined primarily by its instruction, expected content type, and insertion anchor.

---

## 2. Core Architecture Principles

### 2.1 Preserve meaning before improving language

The system may improve grammar, readability, terminology, and formatting, but it must not change:

- Regulatory or procedural meaning.
- Mandatory, optional, or prohibited actions.
- Roles and responsibilities.
- Approval conditions.
- Process sequence.
- Conditions, branches, or exceptions.
- Numerical values.
- Dates, deadlines, frequencies, and durations.
- Acceptance criteria.
- Safety statements.
- Record-retention requirements.
- System, form, document, or policy references.

Example:

```text
Source:
QA shall approve the deviation before closure.

Allowed rewrite:
Quality Assurance (QA) must approve the deviation before closure.

Not allowed:
Quality Assurance should review the deviation before closure.
```

The disallowed version weakens the obligation and changes approval into review.

### 2.2 Preserve source order before applying semantic matching

The source SOP is an ordered process, not an unordered collection of facts. Therefore:

- Source sections are processed in reading order.
- Procedure steps retain their sequence.
- Tables retain intended row and column reading order.
- Conditions, prerequisites, branches, and outcomes remain linked.
- Semantic matching occurs inside an already approved section mapping.
- Retrieval is secondary and cannot define the main procedure sequence.

### 2.3 Separate planning from drafting

The planners determine where content belongs. The drafter determines how approved content should be written.

```text
Section Planner:
Which source section belongs in which target section?

Slot Planner:
Which source units answer each target instruction?

Drafter:
How should the mapped evidence be expressed according to GWP?
```

The planners must not freely create final target content.

### 2.4 Define semantic slots independently of icons

Not all target instructions have icons. Therefore, the final slot definition is:

```text
Target section
+ instruction or placeholder
+ expected content type
+ insertion anchor
+ optional icon metadata
```

An icon may help classify or display a slot, but it is not required.

### 2.5 Keep LLM context bounded

Each planning or drafting call receives only the material required for the current target section or coherent slot group. The system must not send all previous generated output into every subsequent call.

### 2.6 Prefer an explicit gap over invented content

If no source evidence exists for a target slot, the system records a gap. It must not invent plausible content or silently label the slot as not applicable.

---

## 3. Inputs

### 3.1 Source SOP or GP document

The legacy document containing authoritative content.

### 3.2 Target Word template

The target structure and layout, including:

- Sections and headings.
- Instructions.
- Placeholders.
- Icons where present.
- General narrative areas.
- Tables and content controls.
- Styles and formatting.
- Headers, footers, and cover pages.

### 3.3 Writing guide

The approved GWP and formatting rules governing the migrated content.

---

## 4. GWP Rule Classification

The writing guide should be classified instead of supplied wholesale to every agent.

### 4.1 Structural rules

Used during section and slot planning when they affect placement.

```text
STR-001: Separate responsibilities from procedure steps.
STR-002: Consolidate definitions in the Definitions section.
STR-003: Keep warnings adjacent to the activity they constrain.
STR-004: Place references in the approved reference section.
```

### 4.2 Style and language rules

Used mainly during drafting.

```text
STY-001: Use active voice when identifying responsibility.
STY-002: Use present tense for executable instructions.
STY-003: Define abbreviations on first use.
STY-004: Apply approved terminology consistently.
STY-005: Use concise and unambiguous sentences.
```

### 4.3 Content-preservation rules

Used by drafting and validation.

```text
PRES-001: Preserve numerical values exactly.
PRES-002: Preserve dates, durations, frequencies, and deadlines.
PRES-003: Do not infer absent roles or responsibilities.
PRES-004: Preserve obligation strength such as must, shall, may, and should.
PRES-005: Preserve conditions, exceptions, and approval dependencies.
```

### 4.4 Formatting rules

Used during document assembly.

```text
FMT-001: Use numbered lists for executable procedures.
FMT-002: Apply the corporate heading hierarchy.
FMT-003: Preserve approved warning layouts and icons.
FMT-004: Apply required table and paragraph styles.
```

---

## 5. Canonical Source Model

The source document is parsed into sections and traceable units in human reading order.

```json
{
  "document_id": "SRC-DOC-001",
  "sections": [
    {
      "source_section_id": "SRC-4.2",
      "heading": "Deviation Processing",
      "section_order": 42,
      "units": [
        {
          "source_unit_id": "SRC-4.2-U001",
          "source_order": 1,
          "unit_type": "procedure_step",
          "text": "The Process Owner creates a deviation record.",
          "location": {
            "page": 12,
            "paragraph": 1
          }
        }
      ]
    }
  ]
}
```

A source unit can be:

- A paragraph.
- A numbered step.
- A bullet item.
- A warning or note.
- A table row or logical group of cells.
- A heading-associated statement.
- A figure caption.
- A reference entry.

The parser must preserve:

- Heading hierarchy.
- Section order.
- Paragraph order.
- List numbering and nesting.
- Table row order.
- Intended table column reading order.
- Warning-to-step relationships.
- Page and section boundaries.

---

## 6. Canonical Target Template Model

The target template is parsed into sections and semantic instruction slots.

```json
{
  "template_id": "TGT-TEMPLATE-003",
  "sections": [
    {
      "target_section_id": "TGT-3",
      "heading": "Performing the Process",
      "display_order": 3,
      "slots": [
        {
          "target_slot_id": "TGT-3-RESPONSIBILITY",
          "instruction": "Who performs this activity?",
          "content_type": "responsibility",
          "icon": {
            "type": "person",
            "relationship": "visual_marker"
          },
          "required": true,
          "display_order": 1,
          "insertion_anchor": "CC_TGT_3_RESPONSIBILITY",
          "instruction_behavior": "retain_as_label"
        },
        {
          "target_slot_id": "TGT-3-INPUTS",
          "instruction": "Describe the inputs required before beginning the process.",
          "content_type": "process_input",
          "icon": null,
          "required": true,
          "display_order": 2,
          "insertion_anchor": "CC_TGT_3_INPUTS",
          "instruction_behavior": "replace"
        },
        {
          "target_slot_id": "TGT-3-STEPS",
          "instruction": "What steps must be completed?",
          "content_type": "ordered_procedure",
          "icon": null,
          "required": true,
          "display_order": 3,
          "insertion_anchor": "CC_TGT_3_STEPS",
          "instruction_behavior": "retain_as_label"
        },
        {
          "target_slot_id": "TGT-3-TIMING",
          "instruction": "When must this activity be completed?",
          "content_type": "timing",
          "icon": {
            "type": "clock",
            "relationship": "visual_marker"
          },
          "required": false,
          "display_order": 4,
          "insertion_anchor": "CC_TGT_3_TIMING",
          "instruction_behavior": "retain_as_label"
        },
        {
          "target_slot_id": "TGT-3-OUTPUTS",
          "instruction": "Explain the expected output of the process.",
          "content_type": "process_output",
          "icon": null,
          "required": false,
          "display_order": 5,
          "insertion_anchor": "CC_TGT_3_OUTPUTS",
          "instruction_behavior": "replace"
        }
      ]
    }
  ]
}
```

### 6.1 Required slot fields

```text
target_slot_id
The stable slot identifier.

target_section_id
The parent target section.

instruction
The question or authoring direction the content must answer.

content_type
A normalized semantic category.

insertion_anchor
The exact location where content is inserted.

required
Whether the template requires the slot to be resolved.

instruction_behavior
Whether the instruction remains, is replaced, or is hidden.
```

### 6.2 Optional slot fields

```text
icon
Optional visual metadata.

display_order
The visual or reading order within the target section.

formatting_profile
The paragraph, list, table, or warning style to apply.
```

### 6.3 Suggested content types

```text
purpose
scope
definition
responsibility
prerequisite
process_input
ordered_procedure
decision
timing
frequency
duration
restriction
warning
expected_output
record
reference
supporting_information
```

---

## 7. Instruction Behavior

Template instructions do not all behave the same way in the final document.

### 7.1 `retain_as_label`

The instruction remains visible as a heading or label.

```text
Who performs this activity?

The Process Owner submits the deviation.
Quality Assurance approves the deviation.
```

### 7.2 `replace`

The instruction is authoring guidance and is replaced by generated content.

```text
Template:
[Describe any inputs required before starting this process.]

Final:
The approved deviation request and supporting records are required before review begins.
```

### 7.3 `hide_after_population`

The instruction is retained in the template for authoring but hidden or removed from the exported version after successful population.

### 7.4 `retain`

The instruction remains unchanged and generated content is inserted at a separate adjacent anchor.

The behavior must be part of the template metadata. The LLM should not decide whether template instructions remain visible.

---

## 8. Slot Detection Strategy

The template parser must not begin by searching for icons. It should detect content regions and insertion anchors in the following priority order:

```text
1. Word content-control tags
2. Explicit placeholders
3. Table-cell relationships
4. Dedicated instruction paragraph styles
5. Known instructional phrases
6. Paragraph proximity
7. Icons as supporting metadata
```

### 8.1 Preferred content controls

Use stable tags such as:

```text
CC_TGT_3_RESPONSIBILITY
CC_TGT_3_INPUTS
CC_TGT_3_STEPS
CC_TGT_3_TIMING
CC_TGT_3_OUTPUTS
```

### 8.2 Table-based instructions

If the template uses tables, associate the instruction cell and destination cell using the same row or an explicit template configuration.

```json
{
  "instruction_location": {
    "table_index": 4,
    "row_index": 2,
    "column_index": 2
  },
  "insertion_location": {
    "table_index": 4,
    "row_index": 2,
    "column_index": 3
  }
}
```

### 8.3 Avoid proximity-only mapping

Paragraph proximity may fail with:

- Empty spacer paragraphs.
- Merged table cells.
- Nested tables.
- Notes between instruction and placeholder.
- Floating icons or text boxes.
- Multiple instructions in one visual block.

Templates intended for automated migration should be normalized with stable content controls or table anchors.

---

## 9. End-to-End Workflow

```text
1. Create migration job
2. Parse and normalize source, template, and writing guide
3. Classify GWP rules
4. Create protected-fact registry
5. Create section-wise bridge plan
6. Validate and optionally review section plan
7. Create semantic instruction-slot plan within each mapped target section
8. Validate and optionally review slot plan
9. Draft all related slots in a target section
10. Run deterministic and semantic validation
11. Repair only failed slots
12. Populate a copy of the target Word template
13. Run document-wide reconciliation
14. Apply hard quality gates
15. Perform risk-based human review
16. Save versioned result and export the Word document
```

---

## 10. Phase 1: Migration Job Creation

```http
POST /api/v1/migrations
```

```json
{
  "sop_id": "source-123",
  "template_id": "template-456",
  "writing_guide_id": "guide-789",
  "mode": "review"
}
```

The backend:

1. Validates input artifacts.
2. Creates an immutable job ID.
3. Sets the status to `PENDING`.
4. Starts background processing.
5. Returns the job ID immediately.

---

## 11. Phase 2: Parsing and Normalization

### 11.1 Source parsing

Extract ordered headings, paragraphs, lists, tables, notes, warnings, references, and source locations. Assign stable source IDs.

### 11.2 Template parsing

Extract target sections, instructions, placeholders, optional icons, insertion anchors, slot order, slot requirements, and instruction behavior.

### 11.3 Writing-guide parsing

Classify rules into structural, style, preservation, and formatting groups.

### 11.4 Protected-fact registry

```json
{
  "protected_values": [
    "95%",
    "five business days",
    "30 calendar days",
    "Version 4.2"
  ],
  "protected_obligations": [
    {
      "source_unit_id": "SRC-4.2-U007",
      "statement": "The Process Owner must not close the deviation until Quality Assurance approves it.",
      "modality": "mandatory_prohibition"
    }
  ],
  "approved_terms": {
    "Quality Assurance": "QA",
    "Corrective and Preventive Action": "CAPA"
  }
}
```

---

## 12. Phase 3: Level 1 Section-Wise Mapping

The Section Planner answers:

> Which complete source section, or ordered group of source sections, belongs in each target section?

Example:

```json
{
  "target_section_id": "TGT-3",
  "source_section_ids": [
    "SRC-4.2",
    "SRC-4.3"
  ],
  "mapping_type": "merge",
  "source_order": [42, 43],
  "ordering_rule": "preserve_source_order",
  "reason": "The sections jointly describe deviation creation, review, correction, approval, and closure.",
  "status": "mapped",
  "confidence": 0.93
}
```

Supported mapping types:

```text
one_to_one
merge
split
move
copy
rewrite
omit
unresolved
```

### 12.1 Section-plan validation

Validate that:

- Every mandatory target section has a mapping or explicit gap.
- Every migratable source section is accounted for.
- Merged sections retain approved order.
- Split mappings identify their semantic scope.
- Omissions include justification and approval.
- Tables, warnings, references, and appendices are not silently lost.

For regulated migration, target 100 percent accounted source content. Every source unit must be mapped, intentionally omitted, duplicated with purpose, marked not applicable by an authorized decision, or escalated.

---

## 13. Phase 4: Level 2 Semantic Instruction-Slot Mapping

After section mapping, the Slot Planner works only within each approved source-to-target section pair.

Its question is:

> Which ordered source units answer each instruction or placeholder in this target section?

The planner receives:

```text
Target section definition
+ all semantic slots
+ instructions and optional icons
+ mapped source section content in original order
+ structural rules
+ protected facts
+ mapping constraints
```

### 13.1 Slot-plan example

```json
{
  "target_section_id": "TGT-3",
  "source_section_ids": ["SRC-4.2"],
  "slot_mappings": [
    {
      "target_slot_id": "TGT-3-RESPONSIBILITY",
      "icon_type": "person",
      "instruction": "Who performs this activity?",
      "source_unit_ids": [
        "SRC-4.2-U001",
        "SRC-4.2-U003",
        "SRC-4.2-U006"
      ],
      "extraction_scope": ["actor", "responsibility"],
      "migration_action": "extract_and_consolidate",
      "status": "mapped"
    },
    {
      "target_slot_id": "TGT-3-INPUTS",
      "icon_type": null,
      "instruction": "Describe the inputs required before beginning the process.",
      "source_unit_ids": ["SRC-4.2-U000"],
      "extraction_scope": ["input", "prerequisite"],
      "migration_action": "extract_and_rewrite",
      "status": "mapped"
    },
    {
      "target_slot_id": "TGT-3-STEPS",
      "icon_type": null,
      "instruction": "What steps must be completed?",
      "source_unit_ids": [
        "SRC-4.2-U001",
        "SRC-4.2-U002",
        "SRC-4.2-U003",
        "SRC-4.2-U004",
        "SRC-4.2-U005",
        "SRC-4.2-U006",
        "SRC-4.2-U007"
      ],
      "extraction_scope": ["action", "condition", "dependency"],
      "ordering_rule": "preserve_source_order",
      "migration_action": "rewrite_as_ordered_procedure",
      "status": "mapped"
    },
    {
      "target_slot_id": "TGT-3-TIMING",
      "icon_type": "clock",
      "instruction": "When must this activity be completed?",
      "source_unit_ids": ["SRC-4.2-U002"],
      "extraction_scope": ["timing", "deadline"],
      "migration_action": "extract_and_rewrite",
      "status": "mapped"
    },
    {
      "target_slot_id": "TGT-3-OUTPUTS",
      "icon_type": null,
      "instruction": "Explain the expected output of the process.",
      "source_unit_ids": ["SRC-4.2-U007"],
      "extraction_scope": ["output", "record"],
      "migration_action": "extract_and_rewrite",
      "status": "mapped"
    }
  ]
}
```

### 13.2 One source unit may support multiple slots

A sentence may contain responsibility, timing, and restriction information. It may therefore support multiple slots while retaining one source reference.

```text
Source:
The Process Owner submits the deviation within five business days and must not close it until Quality Assurance gives approval.

Responsibility slot:
The Process Owner submits the deviation. Quality Assurance approves it.

Timing slot:
Submit the deviation within five business days.

Restriction slot:
Do not close the deviation before Quality Assurance approval.
```

### 13.3 Primary narrative versus supporting slots

Each process-oriented section should ideally contain a primary ordered procedure or narrative slot. Other slots provide focused summaries.

```text
Primary ordered procedure:
Preserves chronology, decisions, branches, and dependencies.

Responsibility slot:
Summarizes actors and responsibilities.

Timing slot:
Highlights timing, frequency, or deadline.

Restriction slot:
Highlights warnings, conditions, or prohibitions.

Input and output slots:
Identify prerequisites and expected results.
```

This ensures semantic slots improve readability without replacing the complete sequence.

### 13.4 Content with no matching slot

Use a general supporting-information slot if available. Otherwise, flag the content for plan review. Do not force it into an unrelated instruction or discard it.

### 13.5 Slot with no supporting content

```json
{
  "target_slot_id": "TGT-3-TIMING",
  "source_unit_ids": [],
  "migration_action": "none",
  "status": "source_content_not_found",
  "requires_human_review": true
}
```

Distinguish:

```text
source_content_not_found:
No authoritative source evidence was found.

not_applicable:
An approved rule or reviewer explicitly determined that the slot does not apply.
```

---

## 14. Limited Role of Retrieval

Retrieval is not the primary mapping method because independent paragraph retrieval may disrupt process order and readability.

Primary mechanism:

```text
Ordered source traversal
-> Section mapping
-> Slot mapping within the mapped source scope
```

Retrieval may be used secondarily to:

- Locate definitions.
- Resolve explicit cross-references.
- Find centrally defined responsibilities.
- Find a general warning explicitly applicable to the process.
- Fill a reviewed gap after the primary map exists.

Any outside evidence must be labeled as cross-section support and must not silently change the primary procedure sequence.

---

## 15. Phase 5: GWP-Controlled Drafting

The preferred drafting unit is one target section plus all related semantic slots.

The drafter receives:

```text
Target section and slot definitions
+ approved section mapping
+ approved slot mappings
+ mapped source units in original order
+ applicable GWP rules
+ preservation rules
+ protected facts and terminology
+ bounded document memory
+ targeted repair feedback, when applicable
```

### 15.1 Bounded document memory

Do not pass every previous generated section. Use a compact memory object containing approved terminology, roles, established facts, adjacent-section summaries, and relevant cross-references.

```json
{
  "approved_terms": {
    "Quality Assurance": "QA"
  },
  "roles": [
    "Process Owner",
    "Quality Assurance"
  ],
  "established_facts": [
    {
      "fact_id": "FACT-018",
      "statement": "QA approval is required before deviation closure.",
      "source_unit_id": "SRC-4.2-U007"
    }
  ],
  "adjacent_section_summaries": [
    {
      "target_section_id": "TGT-2",
      "summary": "Defines scope and affected functions."
    }
  ]
}
```

### 15.2 Draft output

```json
{
  "target_section_id": "TGT-3",
  "slots": [
    {
      "target_slot_id": "TGT-3-RESPONSIBILITY",
      "content": [
        "The Process Owner creates, submits, updates, and closes the deviation.",
        "Quality Assurance (QA) reviews and approves the deviation."
      ],
      "source_unit_ids": [
        "SRC-4.2-U001",
        "SRC-4.2-U002",
        "SRC-4.2-U003",
        "SRC-4.2-U006",
        "SRC-4.2-U007"
      ]
    },
    {
      "target_slot_id": "TGT-3-STEPS",
      "content": [
        "Create the deviation record.",
        "Submit the deviation within five business days.",
        "QA reviews the deviation.",
        "If information is incomplete, provide the missing information.",
        "QA approves the deviation.",
        "Close the deviation after QA approval."
      ],
      "ordering_rule_applied": "preserve_source_order"
    },
    {
      "target_slot_id": "TGT-3-TIMING",
      "content": [
        "Submit the deviation within five business days."
      ],
      "source_unit_ids": ["SRC-4.2-U002"]
    }
  ],
  "unresolved_items": []
}
```

---

## 16. Context and Batching

Do not use a fixed rule such as two sections per batch. Use token-aware and complexity-aware boundaries based on:

- Estimated input token count.
- Expected output size.
- Number of source units.
- Number of target slots.
- Table complexity.
- Cross-reference density.
- Risk classification.
- Model context limit and safety reserve.

Each call should receive only:

```text
Mapped source sections
+ current target section
+ current semantic slots
+ applicable rules
+ protected facts
+ bounded memory
+ relevant cross-references
```

If one section is too large, split it into coherent process blocks or related slot groups while preserving order and section identity.

---

## 17. Phase 6: Validation

Quality control combines deterministic validation, semantic criticism, and human review.

### 17.1 Deterministic checks

#### Structure

- Required target sections exist.
- Required slots are populated or explicitly flagged.
- Content is written to the correct anchor.
- Instruction behavior is applied correctly.
- No unexpected section or slot is created.

#### Traceability

- Every substantive target statement has supporting source IDs.
- Every source unit is accounted for.
- Intentional reuse across slots is recorded.
- Omissions have justification and approval.

#### Preservation

- Numbers, dates, durations, frequencies, and deadlines remain unchanged.
- Obligation strength remains unchanged.
- Role names and system names remain correct.
- Ordered steps retain sequence.

#### Formatting

- Required list and table structures are preserved.
- Cross-references resolve.
- Required placeholders are removed or retained according to configured behavior.
- No unresolved `[TBD]` markers remain unnoticed.

### 17.2 Semantic critic

The semantic critic checks:

- Meaning preservation.
- Relevance to the slot instruction.
- Ambiguity.
- Logical flow.
- Redundancy.
- Contradiction.
- Correct treatment of conditions and exceptions.
- Natural GWP application.

### 17.3 Order and dependency validation

Validate action sequence, prerequisites, approval dependencies, conditional branches, exception paths, and attachment of timing requirements to the correct action.

---

## 18. Phase 7: Targeted Repair

Do not regenerate the entire document because one slot failed.

```text
Validation issue
-> Identify affected section and slot
-> Load approved source evidence and rules
-> Repair only that slot
-> Rerun slot validation
-> Rerun affected section checks
-> Rerun impacted document-wide checks
```

Failure handling:

```text
Invalid JSON:
Retry once with schema-repair instructions.

Temporary provider error:
Retry with controlled backoff.

Unsupported claim:
Repair the affected slot and revalidate.

Missing source information:
Do not retry generation; require human review.

Conflicting source statements:
Require human resolution.

Repeated semantic failure:
Set HUMAN_REVIEW_REQUIRED.
```

---

## 19. Phase 8: Word Document Assembly

Populate a copy of the original target Word template.

```text
Original target .docx
-> Copy to output version
-> Locate content controls or stable anchors
-> Apply instruction behavior
-> Insert validated slot content
-> Preserve optional icons and adjacent instructions
-> Preserve layout, styles, and tables
-> Update cross-references and table of contents
-> Save as a new .docx
```

The renderer, not the LLM, controls physical placement.

Preserve:

- Icons where present.
- Instructions configured to remain visible.
- Corporate styles and fonts.
- Tables and cell dimensions.
- Borders and spacing.
- Headers and footers.
- Section breaks and page orientation.
- Cover pages and fixed template text.

Markdown may be used as a preview or audit representation, but it should not be the sole authoritative output for a layout-rich Word template.

---

## 20. Phase 9: Document-Wide Reconciliation

After assembly, check:

- Terminology consistency.
- Role-name consistency.
- Abbreviation definitions.
- Duplicate or contradictory content.
- Broken cross-references.
- Step and section numbering.
- Definition usage.
- Reference completeness.
- Consistency between ordered narrative and supporting slots.

The reconciliation component should propose traceable patches rather than freely rewriting the document.

---

## 21. Quality Gates

Hard gates must pass before automatic completion:

```text
Critical unsupported claims = 0
Unexplained numerical changes = 0
Missing mandatory sections = 0
Missing mandatory slots = 0
Broken mandatory cross-references = 0
Unaccounted source requirements = 0
Unresolved high-risk issues = 0
Sequence or dependency violations = 0
```

After hard gates pass, softer measures may evaluate:

- GWP compliance.
- Terminology consistency.
- Semantic fidelity.
- Template adherence.
- Readability.
- Redundancy.

Thresholds should be calibrated using approved migration examples.

High-risk content should require direct human review, including:

- Acceptance criteria.
- Safety warnings.
- Regulatory commitments.
- Approval responsibilities.
- Timelines and deadlines.
- Numerical limits.
- Data-retention periods.
- Escalation conditions.
- Mandatory prohibitions.

---

## 22. Status Model

```text
PENDING
PARSING
PLANNING_SECTIONS
SECTION_PLAN_REVIEW_PENDING
PLANNING_SLOTS
SLOT_PLAN_REVIEW_PENDING
DRAFTING
VALIDATING
REPAIRING
ASSEMBLING
RECONCILING
QUALITY_REVIEW
HUMAN_REVIEW_REQUIRED
COMPLETED
COMPLETED_WITH_WARNINGS
MANUALLY_EDITED
FAILED_TECHNICAL
CANCELLED
```

`FAILED_TECHNICAL` means the workflow could not complete because of a parser, database, provider, corruption, or similar technical failure.

`HUMAN_REVIEW_REQUIRED` means output exists, but correctness could not be established because of missing, conflicting, ambiguous, or repeatedly failing evidence.

---

## 23. Versioning and Audit

Version every material artifact:

```text
Source version
Target-template version
Writing-guide version
Parsed source model
Parsed target model
Section-plan version
Human-edited section plan
Slot-plan version
Human-edited slot plan
Drafted slot version
Repaired slot version
Assembled document
Reconciled document
Manually edited document
Exported document
```

Record:

- Job ID.
- Source and template hashes.
- Writing-guide version.
- Rule IDs.
- Prompt-template version.
- Model deployment and configuration.
- Source-unit IDs supplied.
- Target section and slot IDs.
- LLM output.
- Token usage and latency.
- Retry count.
- Validation results.
- Repair reasons.
- Reviewer identity and timestamps.
- Human overrides.
- Diffs between versions.

---

## 24. Service Responsibilities

### Migration Orchestrator

Manages state, component calls, retries, escalation, persistence, and audit events.

### Source Parser

Extracts ordered sections and traceable source units.

### Template Parser

Identifies target sections, instructions, placeholders, optional icons, instruction behavior, and insertion anchors.

### Writing-Guide Parser

Classifies rules and assigns stable rule IDs.

### Section Planner

Maps ordered source sections to target sections.

### Slot Planner

Maps source units inside approved section boundaries to semantic instruction slots.

### Drafter

Rewrites approved evidence according to applicable GWP.

### Deterministic Validator

Checks structure, traceability, values, obligations, order, completeness, and anchors.

### Semantic Critic

Checks fidelity, logic, relevance, ambiguity, and readability.

### Repair Agent

Repairs only the affected slot or section.

### Word Renderer

Populates a copy of the original template and preserves its layout and visual elements.

---

## 25. API Outline

```http
POST /api/v1/migrations
GET /api/v1/migrations/{job_id}
GET /api/v1/migrations/{job_id}/section-plan
PATCH /api/v1/migrations/{job_id}/section-plan
POST /api/v1/migrations/{job_id}/section-plan/approve
GET /api/v1/migrations/{job_id}/slot-plan
PATCH /api/v1/migrations/{job_id}/slot-plan
POST /api/v1/migrations/{job_id}/slot-plan/approve
GET /api/v1/migrations/{job_id}/validation
GET /api/v1/migrations/{job_id}/traceability
GET /api/v1/migrations/{job_id}/audit
PATCH /api/v1/migrations/{job_id}/sections/{target_section_id}/slots/{target_slot_id}
GET /api/v1/export/word/{job_id}
```

---

## 26. Worked Example

### Source section

```text
4.2 Deviation Processing

1. The Process Owner creates a deviation record.
2. The Process Owner submits the deviation within five business days.
3. Quality Assurance reviews the deviation.
4. If information is incomplete, Quality Assurance returns it to the Process Owner.
5. The Process Owner provides the missing information.
6. Quality Assurance approves the deviation.
7. The Process Owner closes the deviation after Quality Assurance approval.
```

### Target section

```text
3. Performing the Process

[Person icon] Who performs this activity?

Describe the inputs required before beginning the process.

What steps must be completed?

[Clock icon] When must this activity be completed?

Explain the expected output of the process.

What restrictions apply?
```

Some instructions have icons and others do not. All are semantic slots.

### Section mapping

```json
{
  "source_section_ids": ["SRC-4.2"],
  "target_section_id": "TGT-3",
  "mapping_type": "one_to_one",
  "ordering_rule": "preserve_source_order",
  "status": "mapped"
}
```

### Slot mapping

```text
SRC-4.2-U001 through U007 -> Ordered procedure slot
SRC-4.2-U001 through U007 -> Responsibility slot
SRC-4.2-U002 -> Timing slot
SRC-4.2-U007 -> Restriction slot
No evidence -> Inputs slot, marked source_content_not_found
SRC-4.2-U007 -> Output slot, if closure is the documented output
```

### Drafted content

```text
Who performs this activity?
The Process Owner creates, submits, updates, and closes the deviation.
Quality Assurance (QA) reviews and approves the deviation.

Inputs
Source content not found. Human review required.

Procedure
1. Create the deviation record.
2. Submit the deviation within five business days.
3. QA reviews the deviation.
4. If information is incomplete, provide the missing information.
5. QA approves the deviation.
6. Close the deviation after QA approval.

When must this activity be completed?
Submit the deviation within five business days.

Expected output
An approved and closed deviation record.

Restriction
Do not close the deviation before QA approval.
```

### Validation expectations

```text
Source order preserved: Yes
Deadline preserved: Yes
Approval requirement preserved: Yes
Closure dependency preserved: Yes
Unsupported input invented: No
Icon required for every slot: No
Every generated statement traceable: Yes
```

---

## 27. Failure Modes

### Unmapped source section

Mark as `unmapped_source_content`; do not omit automatically.

### Target slot without evidence

Mark as `source_content_not_found`; do not invent content.

### Conflicting source statements

Mark as `conflicting_source` and require human resolution.

### Source section split across target sections

Record explicit semantic scope and account for every source unit.

### Multiple source sections merged

Preserve approved source-section order and check duplicates and contradictions.

### Source content matches no instruction

Use a general slot or request plan review. Do not force it into an unrelated slot.

### Instruction has no icon

Treat it as a normal semantic slot if it has a valid instruction and insertion anchor.

### Icon has no clear instruction

Do not infer meaning from the icon alone. Use configured metadata or require template review.

### Instruction has no stable insertion anchor

Normalize the template by adding a content control or stable table-cell anchor.

### Repeated repair failure

Stop automatic repair and set `HUMAN_REVIEW_REQUIRED`.

---

## 28. Implementation Sequence

### Phase A: Core

1. Parse source sections and ordered source units.
2. Parse target sections and semantic instruction slots.
3. Support slots with and without icons.
4. Add stable Word insertion anchors.
5. Build structured section plans.
6. Build slot plans within approved section boundaries.
7. Draft all related slots in one target section.
8. Populate the original target template.

### Phase B: Safety and traceability

1. Attach source IDs to every generated statement.
2. Extract protected facts, numbers, dates, roles, and modalities.
3. Add deterministic validation.
4. Add targeted repair.
5. Add immutable artifact versions.
6. Separate technical failure from human-review-required outcomes.

### Phase C: Quality and scale

1. Add bounded document memory.
2. Add token-aware section splitting.
3. Add document-wide reconciliation.
4. Add risk-based review.
5. Calibrate confidence and quality thresholds using approved migrations.
6. Build a golden evaluation set for section mapping, slot mapping, drafting, and validation.

---

## 29. Final Architecture Summary

```text
Source SOP in reading order
        ↓
Parse sections and traceable source units
        ↓
Parse target template
        ↓
Identify sections, instructions, placeholders, optional icons, and anchors
        ↓
Section Planner
Source section(s) -> Target section
        ↓
Review and approve section plan
        ↓
Slot Planner
Ordered source units -> Semantic instruction slots
        ↓
Review and approve slot plan
        ↓
Section Drafter
Approved evidence + applicable GWP -> Slot content
        ↓
Deterministic validation + semantic criticism
        ↓
Targeted slot repair
        ↓
Populate a copy of the original Word template
        ↓
Document-wide reconciliation + hard quality gates
        ↓
Risk-based human review
        ↓
Versioned Word export with complete traceability
```

The central rule is:

> First map source sections to target sections. Then, within each approved section pair, map ordered source content to semantic instruction slots. Icons are optional visual metadata and are not required for a slot to exist.

This design preserves readability and process order while supporting precise placement for instructions with icons, instructions without icons, general narrative areas, and structured placeholders. It also keeps LLM context bounded and provides strong controls against hallucination, omission, and unintended changes to regulated content.

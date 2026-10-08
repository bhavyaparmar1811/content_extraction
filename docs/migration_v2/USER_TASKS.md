# Migration v2: My Tasks and How to Check Progress

Last updated: 2026-10-08

## Where things stand

- **Done:** Phases 0–8 (contracts, SOP and template extraction, GWP rules, job API, preservation checks, section planner, slot planner) and the SOP inspection tool.
- **GWP rules are now extracted by the app** from the guide (no hand-curated rules): `BI-VQD-24416-G` v3, 70 candidate rules waiting for your review (task 3).
- **Next:** Phase 9, the drafter.
- **Code:** branch `features/migration-v2`; Phase 8 is not committed yet (last commit `088b1fa`).

---

## My tasks

### A. Review the slot plans (not blocking Phase 9)

- [x] ~~1. Add new sample files~~ — none are coming (2026-10-08); Phase 8 was built and checked on the 3 samples.

- [ ] **2. Review the inspection reports**
  - `documents/reports/index.html`: placement mode (no GWP). `documents/reports_gwp/index.html`: with the app-extracted GWP rules (candidates previewed as approved).
  - New in each SOP page: **Slot plan** (which passages go into which template slot, gaps, callout boxes, "This SOP…" choices) and **GWP rules** (which rules went to the LLM, and which rules each slot hands to the drafter).
  - The WARNs are review items (a figure in DEFINITIONS, long notes beside tables, LLM notes). All golden comparisons PASS.
  - *Tell Claude:* which placements or callouts look wrong, or "slot plans look fine."

### B. GWP (needed only for migrations that use a GWP; not blocking Phase 8)

- [ ] **3. Review and approve the GWP rules the app extracted** — `documents/GWP/BI-VQD-24416-G_v3_rules.json` (same file as `data/gwp/BI-VQD-24416-G_v3_rules.json` in the app's GWP store)
  - 70 rules with `"status": "candidate"` (16 STR, 24 STY, 30 FMT). The 6 baseline `PRES-00x` rules are built in and already approved.
  - The report `BI-VQD-24416-G_v3_report.json` lists what was skipped (with reasons), merged as duplicate, or downgraded.
  - For each candidate: approve, edit its `text`, or delete it. Some near-duplicates are left for you (STY-007/008, FMT-022/023, FMT-003/004/005).
  - Ways to do it: `POST /api/v1/gwp/BI-VQD-24416-G/approve` (all, or `rule_ids`), `PUT /api/v1/gwp/BI-VQD-24416-G/rules` with an edited file, or say "approve all GWP rules".
  - Re-extract any time: `venv\Scripts\python scripts\extract_gwp.py --file documents\GWP\BI-VQD-24416-G.pdf --copy-to documents\GWP`
  - The old hand-written set is kept only as a test reference: `BI-VQD-24416-G_curated_rules.json`.

- [ ] **4. Confirm three interpretations of the guide** (as the app extracted them)
  - STY-005: "about two lines" became **at most 20 words** per sentence (the earlier hand-written set used 30).
  - STY-019/STY-020: the readability target is Flesch ≥ 30 and grade level ≤ 12.
  - STY-021: passive voice stays under 10% of sentences.

- [ ] **5. Confirm the "may" rule**
  - The guide says to avoid "may" (STY-014) and use "must"/"should" (STY-011/012). But changing an obligation changes the procedure's meaning.
  - Current choice: "may" is flagged for the reviewer and never rewritten automatically (the built-in PRES-004 wins over style rules).

### C. Template

- [ ] **6. Region decisions for the second template**
  - The first template is done: `Template_Main_GP_Docs.json` has p:26, p:32 and t:7 as conditional, and ROLES has "roles and/or RACI".
  - Do the same for the second template once its inspection report lists the regions.

### D. Housekeeping

- [ ] **7. Push and open a PR** when ready:
  ```powershell
  git push -u origin features/migration-v2
  ```

---

## How to check progress

Run all commands from `D:\Projects\GP DAT\content_extraction`.

### 1. Read the progress log
- `docs/migration_v2/PROGRESS.md` has one entry per phase or session. **The last entry is the current state** and ends with "Next step".
- `docs/migration_v2/IMPLEMENTATION_PLAN.md` has the phase map table, plus a **"Done when"** condition for each phase.

### 2. Ask Claude
- Ask "what stage are we in?" Claude reads the same two files and `git status`.

### 3. Run the tests
```powershell
venv\Scripts\python -m pytest -q
```
- Expected today: **465 passed, 11 skipped**. The count grows with each phase. Any failure means something broke.
- Optional live tests:
  - `$env:SOP_RUN_LLM_TESTS="1"` calls Azure gpt-4o.
  - `$env:SOP_RUN_WORD_TESTS="1"` opens files in Microsoft Word.

### 4. Run the pipeline on real SOPs
```powershell
# every SOP in the folder, rules only (no LLM)
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs

# also run the LLM confirm calls (section and slot planners)
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs --llm

# with the GWP rules the app extracted (candidates previewed as approved)
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs --llm --gwp-rules documents\GWP\BI-VQD-24416-G_v3_rules.json --out documents\reports_gwp

# against another template
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs --template "documents\Templates\<file>.docx" --template-config "documents\template_config\<file>.json"
```
Then open `documents\reports\index.html`. Each SOP gets a page with:
- the checks;
- the section plan;
- the slot plan (passages → template slots, gaps, callout boxes) and the GWP rules used;
- the passages, with values, roles and modal words highlighted;
- the protected facts, the template slots and text completeness;
- the comparison with the golden file.

| Status | Meaning |
|---|---|
| PASS | As expected |
| WARN | Look at it (e.g. a mapping to review, an LLM note) |
| FAIL | Fix before migrating (lost text, blocking plan issue, template not ready, golden mismatch) |
| INFO | Nothing to check |

Each new phase adds its output to this report.

### 5. Check git
```powershell
git log --oneline -5   # one commit per phase
git status             # uncommitted work in progress
```

### 6. Optional: run the app
```powershell
venv\Scripts\activate
uvicorn app.main:app --reload --port 8000
```
- Open `http://localhost:8000/docs` for the API. The v2 endpoints are under `/api/v1/migrations` and `/api/v1/gwp`, and need a login.
- Frontend: `cd frontend` and then `npm run dev`.

---

## Phase map

| Phase | Name | Status |
|---|---|---|
| 0 | Samples, golden set, quick fixes | ✅ Done |
| 1 | v2 data contracts | ✅ Done |
| 2 | SOP extraction: source units | ✅ Done for DOCX; PDF tested on the GWP guide only (no PDF SOPs) |
| 3 | Template slots, anchors, normalization | ✅ Done |
| 4 | GWP ingestion and rule catalog | ✅ Done; rules now extracted by the app (await approval, task 3) |
| 5 | Job store, orchestrator, API | ✅ Done |
| 6 | Protected facts and preservation checks | ✅ Done |
| 7 | Section planner | ✅ Done |
| 8 | Slot planner | ✅ Done |
| 9 | Drafter (GWP or placement mode) | ⏳ Next |
| 10 | Validation, critic, repair, gates | ⬜ |
| 11 | Word renderer v2 | ⬜ |
| 12 | Assembly, cross-references, audit, export | ⬜ |
| 13 | Frontend review UI | ⬜ |
| 14 | Evaluation, calibration, cutover | ⬜ |

---

## Decisions already made

| Date | Decision |
|---|---|
| 2026-10-03 | Template: "This Directive/SOP/..." lines and the competence table are chosen from the SOP's content. Roles tables A and B are "either or both". |
| 2026-10-06 | A GWP is optional. Without one, migration runs in **placement mode**: content goes in as written, with only the template's structure, numbering and callouts applied. Safety checks always run. |
| 2026-10-07 | Golden files accepted for now; changes can come later. |
| 2026-10-07 | SOP content may be sent to the Azure OpenAI gpt-4o deployment. |
| 2026-10-07 | **Empty slots:** an optional slot with no content is removed. A required slot with no content shows a "Source content not found" marker in the review draft, and the reviewer accepts it as N/A or adds content. The final export removes it and its instruction text, and is blocked while any gap is unresolved. Content is never invented to fill a gap. |
| 2026-10-08 | No more SOPs, templates or GWPs: Phase 8 built on the 3 samples. |
| 2026-10-08 | **GWP writing rules are extracted by the app and passed to the LLM, never hard-coded.** Only preservation rules (values, document names and references, obligations) are built in. |

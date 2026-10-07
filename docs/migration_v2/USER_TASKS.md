# Migration v2: My Tasks and How to Check Progress

Last updated: 2026-10-07

## Where things stand

- **Done:** Phases 0–7 (contracts, SOP and template extraction, GWP rules, job API, preservation checks, section planner) and the SOP inspection tool.
- **Next:** Phase 8, the slot planner. It is waiting on task 1 below.
- **Code:** commit `9c75540` on branch `features/migration-v2`, not pushed.

---

## My tasks

### A. Blocking Phase 8

- [ ] **1. Add the new sample files**
  - PDF SOPs and extra DOCX SOPs go in `documents/SOPs/`.
  - The second template goes in `documents/Templates/`.
  - Optional: a region config for it in `documents/template_config/<template name>.json`. Without one, the report lists the template regions that need a decision, and we decide them together.
  - *Tell Claude:* "New samples are in, run the inspection."

- [ ] **2. Review the inspection reports**
  - Run the inspection (see [Check progress, step 4](#4-run-the-pipeline-on-real-sops)) and open `documents/reports/index.html`.
  - Go through every **FAIL** and **WARN**. Claude fixes the defects before Phase 8 starts.
  - *Tell Claude:* which reports look wrong, or "reports look fine."

### B. GWP (needed only for migrations that use a GWP; not blocking Phase 8)

- [ ] **3. Approve the GWP rules** in `documents/GWP/BI-VQD-24416-G_rules.json`
  - There are 40 rules with `"status": "candidate"`. The 6 baseline `PRES-00x` rules are already approved.
  - For each candidate, either set `"status": "approved"`, edit its `text`, or delete it.
  - Or say "approve all GWP rules", and Claude imports and approves them through the API.

- [ ] **4. Confirm three interpretations of the guide**
  - STY-003: "about two lines" means at most 30 words per sentence.
  - STY-019: the readability target is Flesch ≥ 30 and grade level ≤ 12.
  - STY-002: passive voice stays under 10% of sentences.

- [ ] **5. Confirm the "may" rule**
  - The guide says to avoid "may" and use "must"/"should" instead. But changing an obligation changes the procedure's meaning.
  - Current choice: "may" is flagged for the reviewer and never rewritten automatically (PRES-004 wins over STY-009/010/012).

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
- Expected today: **438 passed, 9 skipped**. The count grows with each phase. Any failure means something broke.
- Optional live tests:
  - `$env:SOP_RUN_LLM_TESTS="1"` calls Azure gpt-4o.
  - `$env:SOP_RUN_WORD_TESTS="1"` opens files in Microsoft Word.

### 4. Run the pipeline on real SOPs
```powershell
# every SOP in the folder, rules only (no LLM)
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs

# also run the LLM confirm call
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs --llm

# against another template
venv\Scripts\python scripts\inspect_sop.py --sop documents\SOPs --template "documents\Templates\<file>.docx" --template-config "documents\template_config\<file>.json"
```
Then open `documents\reports\index.html`. Each SOP gets a page with:
- the checks;
- the section plan;
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
| 2 | SOP extraction: source units | ✅ Done for DOCX; PDF untested until the PDF SOPs arrive (task 1) |
| 3 | Template slots, anchors, normalization | ✅ Done |
| 4 | GWP ingestion and rule catalog | ✅ Done (rules await approval, task 3) |
| 5 | Job store, orchestrator, API | ✅ Done |
| 6 | Protected facts and preservation checks | ✅ Done |
| 7 | Section planner | ✅ Done |
| 8 | Slot planner | ⏳ Next (waiting on tasks 1–2) |
| 9 | Drafter (GWP or placement mode) | ⬜ |
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

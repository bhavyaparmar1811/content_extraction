# Known Issues: GP DAT SOP Document Management System

> Snapshot of problems found on 2026-10-02 by reading the codebase. Items marked **[verified]** were re-checked directly in the code. Others come from the exploration pass and should be confirmed before fixing. Translation is deliberately out of scope for now; it is listed only as a readiness note.

## Context

The system migrates SOP documents (PDF/DOCX) onto a newer DOCX template. The pipeline is: parse, AST, chunk and export; template extraction; an LLM-planned migration plus deterministic OXML editing; QA; review. The backend is FastAPI with SQLite, and the frontend is React/Vite. Everything lives under `content_extraction/`.

## Suggested priority

1. Auth, roles and secrets (items 1-4)
2. Document versioning and persistent jobs (5-9)
3. Real workflow and approval backend with audit trail (10-14)
4. Migration content-fidelity checks and tests (15-18)
5. Maintainability clean-up (19-21)

## Critical: security

1. **No auth on core API [verified].** Only `/api/v1/auth/*` enforces auth. Upload, documents, SOPs, templates, migration and review routes are open. Fix: add a shared auth dependency in `app/api/` and apply it at router level. *(2026-10-06: `require_user` in `app/api/auth.py` is that dependency. It guards `/api/v1/migrations` and `/api/v1/gwp`; the older routers are still open.)*
2. **No role enforcement.** Roles exist in `auth.db`, but nothing guards approve/reject, delete or `/admin/reprocessing`.
3. **Hardcoded JWT secret and insecure cookie [verified].** `app/config/settings.py:96` has a dev default secret, and `settings.py:108` sets `auth_cookie_secure=False`. Fix: require the secret from env, and fail startup if it is missing outside dev.
4. **Internal error leakage [verified].** `app/api/templates.py:213` returns `str(e)` in a 500. Use the app's generic exception handlers.

## High: SOP integrity

5. **Re-upload overwrites files on disk.** A new version adds a DB row, but `data/output/{uid}_v2.json` and migrated DOCX files are overwritten. Older `sop_records` rows then point at the wrong content, so there is no real version history.
5a. **Tests write to tracked databases.** Running the suite modifies `data/template_records.db` and `data/sop_records.db`, which gets the new `units_path` column. Restore both with `git checkout -- data/*.db` before committing, or point the tests at `tmp_path`.
6. **Fragile document UID.** `generate_document_id()` in `app/services/extraction/metadata_extractor.py` derives the UID from extracted name/title/number/version. A misparse can create duplicates or collisions. *(2026-10-03: cover-sheet values inside content controls are now read, which fixed BI-VQD-10505-S's version `"."` and type `">"`. The UID derivation itself is unchanged.)*
7. **In-memory job state [verified].** `JobManager.jobs` in `app/services/job_manager.py:34` is a plain dict, so a restart loses all jobs. Concurrency is 1, so batches queue. *(The v2 migration jobs are persisted in `data/migrations.db` and resume after a restart; extraction jobs are unchanged.)*
8. **Template extraction race.** The `extracting` status in `templates.py` can be triggered twice concurrently.
9. **Relative paths and silent template fallback [verified].** `app/api/migration.py:196` uses `Path("data/template_uploads")`, which depends on the working directory. Template selection falls back to the first ready template, so a SOP can be migrated onto the wrong template unnoticed. Use `settings.template_upload_dir` and require an explicit template.

## High: review and approval workflow is largely mocked

10. **Missing backend endpoints.** There are no routes for workflows, issues, versions, suggestions, reprocessing or `/migrations/{id}/sections`. The frontend (and Vite proxy) expects them, so the UI falls back to in-memory mocks. Reprocessing Approve/Reject changes nothing persistent.
11. **Hardcoded versions [verified].** `app/api/review.py:120,124` always returns `workflowVersion=1` and `rowVersion=1`, so the optimistic locking that `frontend/src/lib/api.ts` already sends cannot work. There is no `workflows` table.
12. **Plain textarea editor.** Tiptap and `useAutosave` are installed but unused. Tables, lists and callouts cannot be edited as structure, and nothing autosaves.
13. **Unmounted views.** `CompareView` and `VersionHistory` exist but are not mounted, so there is no side-by-side original vs migrated view.
14. **No approval gate or audit trail.** `PATCH /sops/{id}/status` just sets the status. It does not check QA results or record who approved or when. `audit_events` in `auth.db` is unused for document actions.

## Medium: migration quality

15. **Single LLM planning call.** `section_aligner` decides placement in one call. No retry, confidence threshold or "needs human decision" path was seen. Self-healing passes patch unmapped sections afterwards.
16. **QA measures coverage only.** `MigrationQAReport` (`app/services/migration/migration_validator.py`) does not verify that numbers, units, warnings and table cells are unchanged. Silent content change is the worst failure for SOPs. *(2026-10-06: v2 has deterministic preservation checks in `app/services/migration_v2/quality/` (Phase 6); v3.1 is unchanged.)*
17. **OCR not wired.** `ocr_language="eng"` is configured and pytesseract is installed, but it is never invoked, so scanned PDFs extract poorly.
18. **Test gaps.** Review, SOP and document routes are barely tested. Migrator tests mock the LLM, so plan quality is untested. There are no frontend tests. The README's "66 tests" is stale (about 106 exist across 25 files).

## Medium: maintainability

19. **Duplicate and hand-mirrored types.** `app/schemas/output.py` is not persisted, while `app/schemas/migration.py` (v3.1) is what is written to `*_v2.json`. `frontend/src/types.ts` is hand-copied from the backend. `lib/api.ts` and `lib/apiClient.ts` duplicate each other.
20. **Three SQLite DBs, no cross-DB integrity.** Deleting a template has no effect on SOPs migrated from it, and users are not linked to SOP records.
21. **Dead and stale parts.**
    - The WebSocket `/jobs/{id}/progress` is ignored by the UI (it polls).
    - `embedding_model` is unused.
    - `data/` holds test debris and a duplicate `sop_records.db` in `data/config/`.
    - The README and `Documentation/TECHNICAL_ARCHITECTURE.md` omit the `/templates` router and several auth routes.
    - `/forgot-password` is linked in the UI but does not exist.

## Translation readiness (later, not a current defect)

22. **Language is never detected.** `sop_records.language` is always `'en'`, and `DocumentMetadata` has no language field. `review.py:123` and `frontend/src/pages/ReviewPage.tsx:255` hardcode `fr` as the target [verified]. Translation-mode links point at non-existent `/translations` routes. The output JSON has no segment-level structure yet. The existing design is in `docs/plans/release_1_spec.md` section 16, and `ChainFactory` in `app/services/llm/` is reusable.

## Verification when fixing

- Backend: `pytest` from `content_extraction/`.
- Frontend: `npm run dev` and `npm run lint` in `frontend/`.
- Manual: upload a SOP, migrate it, review it, then re-upload it and confirm earlier versions still open.

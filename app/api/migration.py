"""FastAPI router for document migration endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, Request, status
from fastapi.responses import FileResponse
from loguru import logger

from app.config.settings import Settings, get_settings
from app.core.exceptions import AppError, NotFoundError
from app.schemas.migration import DocxMigrationOutput
from app.services.llm.chain_factory import ChainFactory
from app.services.migration.docx_migrator import DocxMigrator
from app.services.migration.schemas import MigrationResult


router = APIRouter(prefix="/documents", tags=["Migration"])


def _find_extracted_json(document_id: str, settings: Settings, request: Request) -> Path:
    """Resolve the extracted JSON file by document ID or UID."""
    # 1. Exact match with _v2.json
    v2_path = settings.output_dir / f"{document_id}_v2.json"
    if v2_path.exists():
        return v2_path

    # 2. Exact match with .json
    exact_path = settings.output_dir / f"{document_id}.json"
    if exact_path.exists():
        return exact_path

    # 3. Lookup in sop_store by id or uid
    sop_store = getattr(request.app.state, "sop_store", None)
    if sop_store:
        record = None
        if document_id.isdigit():
            record = sop_store.get_record_by_id(int(document_id))
        if not record:
            record = sop_store.get_record_by_uid(document_id)
        if record:
            doc_uid = record.get("document_Uid") or record.get("document_uid")
            if doc_uid:
                candidate_v2 = settings.output_dir / f"{doc_uid}_v2.json"
                if candidate_v2.exists():
                    return candidate_v2
                candidate_reg = settings.output_dir / f"{doc_uid}.json"
                if candidate_reg.exists():
                    return candidate_reg
            if record.get("output_path"):
                rec_path = Path(record["output_path"])
                if rec_path.exists():
                    return rec_path

    # 4. Glob candidate search
    candidates = list(settings.output_dir.glob(f"*{document_id}*.json"))
    if candidates:
        return candidates[0]

    raise NotFoundError(
        f"Extracted JSON not found for document_id '{document_id}'.",
        code="DOCUMENT_NOT_FOUND",
    )


def _find_migration_file(
    dir_path: Path,
    prefix: str,
    document_id: str,
    suffix: str,
    request: Request,
) -> Optional[Path]:
    """Find a saved migration artifact (.docx, plan JSON, qa JSON)."""
    exact = dir_path / f"{prefix}{document_id}{suffix}"
    if exact.exists():
        return exact

    sop_store = getattr(request.app.state, "sop_store", None)
    if sop_store:
        record = None
        if document_id.isdigit():
            record = sop_store.get_record_by_id(int(document_id))
        if not record:
            record = sop_store.get_record_by_uid(document_id)
        if record:
            doc_uid = record.get("document_Uid") or record.get("document_uid")
            if doc_uid:
                rec_file = dir_path / f"{prefix}{doc_uid}{suffix}"
                if rec_file.exists():
                    return rec_file

    candidates = list(dir_path.glob(f"{prefix}*{document_id}*{suffix}"))
    if candidates:
        return candidates[0]

    return None


@router.post("/migrate", response_model=MigrationResult)
async def migrate_document(
    request: Request,
    document_id: str = Form(...),
    template_id: Optional[str] = Form(None),
    template_file: Optional[UploadFile] = File(None),
    settings: Settings = Depends(get_settings),
):
    """Migrate an extracted document JSON into a .docx template.

    - **document_id**: ID or UID of previously extracted document.
    - **template_id**: Optional registered template UID or ID.
    - **template_file**: Optional custom .docx template file. If omitted, uses registered or default template.
    """
    logger.info(f"Migration request received for document_id='{document_id}', template_id='{template_id}'")

    # Locate extracted JSON
    json_path = _find_extracted_json(document_id, settings, request)

    # Load extracted JSON into DocxMigrationOutput
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        extracted = DocxMigrationOutput.model_validate(raw_data)
    except Exception:
        raise AppError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="INVALID_EXTRACTED_JSON",
            message="Failed to parse extracted JSON.",
        )

    # Handle template selection / file
    target_template_path = None
    template_store = getattr(request.app.state, "template_store", None)

    # Sanitize template_id from frontend artifacts like '[object Object]', 'undefined', 'null'
    if template_id:
        clean_id = template_id.strip()
        if clean_id in ("[object Object]", "undefined", "null", "none", ""):
            template_id = None
        else:
            template_id = clean_id

    if template_id:
        record = None
        if template_store:
            if template_id.isdigit():
                record = template_store.get_record_by_id(int(template_id))
            if not record:
                record = template_store.get_record_by_uid(template_id)
            if not record:
                all_records = template_store.get_records()
                record = next(
                    (
                        r
                        for r in all_records
                        if r.get("template_uid", "").lower() == template_id.lower()
                        or r.get("template_name", "").lower() == template_id.lower()
                    ),
                    None,
                )
        if record and record.get("upload_path"):
            target_template_path = Path(record["upload_path"])
            if not target_template_path.exists():
                raise AppError(
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="TEMPLATE_FILE_NOT_FOUND",
                    message=f"Template file not found at: {record['upload_path']}",
                )
        else:
            raise AppError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="TEMPLATE_NOT_FOUND",
                message=f"Template '{template_id}' not found in template records.",
            )
    elif template_file and template_file.filename:
        template_save_path = settings.migration_template_dir / f"upload_{template_file.filename}"
        with open(template_save_path, "wb") as f:
            content = await template_file.read()
            f.write(content)
        target_template_path = template_save_path
    else:
        # 1. Fallback: Find registered ready template in template_store
        if template_store:
            ready_templates = template_store.get_records(status="ready")
            for cand in ready_templates:
                if cand.get("upload_path") and Path(cand["upload_path"]).exists():
                    target_template_path = Path(cand["upload_path"])
                    logger.info(
                        f"Using default ready template '{cand.get('template_name')}' ({cand.get('template_uid')}) at {target_template_path}"
                    )
                    break

        # 2. Fallback: Look in settings.template_upload_dir
        if not target_template_path:
            uploads = list(settings.template_upload_dir.glob("*.docx"))
            if uploads:
                target_template_path = uploads[0]

        # 3. Fallback: Look in settings.migration_template_dir (data/templates)
        if not target_template_path:
            templates = list(settings.migration_template_dir.glob("*.docx"))
            if templates:
                target_template_path = templates[0]

        if not target_template_path:
            raise AppError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="TEMPLATE_NOT_FOUND",
                message="No template selected or uploaded and no default .docx found.",
            )

    output_docx_path = settings.migration_output_dir / f"migrated_{document_id}.docx"
    plan_save_path = settings.migration_output_dir / f"plan_{document_id}.json"
    qa_save_path = settings.migration_output_dir / f"qa_{document_id}.json"

    # Get or create ChainFactory from app state
    chain_factory = getattr(request.app.state, "chain_factory", None)
    if chain_factory is None:
        chain_factory = ChainFactory(settings)

    migrator = DocxMigrator(settings, chain_factory)

    try:
        result = await migrator.migrate(
            extracted=extracted,
            template_path=target_template_path,
            output_path=output_docx_path,
        )

        # Save plan and QA report for persistent retrieval
        with open(plan_save_path, "w", encoding="utf-8") as f:
            json.dump(result.plan.model_dump(), f, indent=2)

        with open(qa_save_path, "w", encoding="utf-8") as f:
            json.dump(result.qa_report.model_dump(), f, indent=2)

        result.download_url = f"/documents/{document_id}/download-docx"
        return result

    except AppError:
        raise
    except Exception:
        logger.exception("Migration failed for '{}'", document_id)
        raise AppError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="MIGRATION_FAILED",
            message="Migration execution failed.",
        )


@router.get("/{document_id}/migration-plan")
async def get_migration_plan(
    document_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Retrieve the LLM-generated MigrationPlan for a document."""
    plan_path = _find_migration_file(
        settings.migration_output_dir, "plan_", document_id, ".json", request
    )
    if not plan_path or not plan_path.exists():
        raise NotFoundError(
            f"Migration plan not found for document_id '{document_id}'",
            code="MIGRATION_PLAN_NOT_FOUND",
        )

    with open(plan_path, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/{document_id}/migration-status")
async def get_migration_status(
    document_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Retrieve migration status and QA report for a document."""
    qa_path = _find_migration_file(
        settings.migration_output_dir, "qa_", document_id, ".json", request
    )
    if not qa_path or not qa_path.exists():
        raise NotFoundError(
            f"QA report not found for document_id '{document_id}'",
            code="MIGRATION_QA_NOT_FOUND",
        )

    with open(qa_path, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/{document_id}/download-docx")
async def download_migrated_docx(
    document_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Download the final migrated .docx file."""
    docx_path = _find_migration_file(
        settings.migration_output_dir, "migrated_", document_id, ".docx", request
    )
    if not docx_path or not docx_path.exists():
        raise NotFoundError(
            f"Migrated .docx not found for document_id '{document_id}'",
            code="MIGRATED_DOCX_NOT_FOUND",
        )

    return FileResponse(
        path=docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=docx_path.name,
    )

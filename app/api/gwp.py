"""GWP (Good Writing Practice) guide API: upload, rule extraction, review and approval (migration v2, Phase 4)."""

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel

from app.api.auth import require_user
from app.config.settings import Settings, get_settings
from app.core.exceptions import AppError, NotFoundError, ValidationError
from app.schemas.auth import ApiFieldError
from app.schemas.v2 import GwpRuleSet
from app.services.extraction.metadata_extractor import SOPMetadataExtractor
from app.services.migration_v2.gwp import service
from app.services.migration_v2.gwp.extractor import GwpRuleExtractor
from app.stores.gwp_store import GwpStore

router = APIRouter(prefix="/api/v1/gwp", tags=["GWP"], dependencies=[Depends(require_user)])


class ApproveRequest(BaseModel):
    rule_ids: Optional[list[str]] = None  # None approves every candidate


def _get_store(request: Request, settings: Settings) -> GwpStore:
    store = getattr(request.app.state, "gwp_store", None)
    if store is None:
        store = GwpStore(settings.project_root / "data" / "gwp_guides.db", settings)
    return store


def _resolve(store: GwpStore, guide: str, version: Optional[int] = None) -> dict[str, Any]:
    """A guide by numeric record id, or by guide_id (latest version unless one is given)."""
    record = None
    if guide.isdigit():
        record = store.get_record_by_id(int(guide))
    if record is None:
        if version is not None:
            record = next((r for r in store.get_records(guide_id=guide) if r["version"] == version), None)
        else:
            record = store.get_latest(guide)
    if record is None:
        raise NotFoundError(f"GWP guide '{guide}' not found", code="GWP_NOT_FOUND")
    return record


def _rules_path(record: dict[str, Any]) -> Path:
    if not record.get("rules_path") or not Path(record["rules_path"]).exists():
        raise NotFoundError(
            f"GWP guide '{record['guide_id']}' v{record['version']} has no rules yet; run extraction or PUT a rule set",
            code="GWP_RULES_NOT_FOUND",
        )
    return Path(record["rules_path"])


def _invalid(problems: list[str]) -> AppError:
    return AppError(
        status_code=422,
        code="GWP_RULES_INVALID",
        message=f"Rule set is invalid ({len(problems)} problem(s))",
        field_errors=[ApiFieldError(field="rules", code="invalid", message=p) for p in problems],
    )


@router.post("")
async def upload_guide(
    request: Request,
    file: UploadFile = File(...),
    guide_name: Optional[str] = Form(None),
    settings: Settings = Depends(get_settings),
):
    """Upload a GWP guide (.docx or .pdf), store it as the next version, and parse it into units."""
    if not file.filename:
        raise ValidationError("Filename is required.", code="FILENAME_REQUIRED")
    extension = Path(file.filename).suffix.lower()
    if extension not in settings.gwp_allowed_extensions:
        raise ValidationError(
            f"Unsupported GWP file type '{extension}'. Allowed: {settings.gwp_allowed_extensions}",
            code="UNSUPPORTED_GWP_FILE_TYPE",
        )
    contents = await file.read()
    if not contents:
        raise ValidationError("Uploaded GWP file is empty.", code="EMPTY_GWP_FILE")
    if len(contents) > settings.max_upload_size_mb * 1024 * 1024:
        raise ValidationError(
            f"GWP file exceeds the maximum size of {settings.max_upload_size_mb} MB.", code="GWP_FILE_TOO_LARGE"
        )

    store = _get_store(request, settings)
    stem = Path(file.filename).stem
    guide_id = SOPMetadataExtractor.sanitize_string(stem) or "GWP"
    version = store.get_next_version(guide_id)
    save_path = settings.gwp_upload_dir / f"{guide_id}_v{version}{extension}"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(contents)

    record = store.create_record(
        guide_id=guide_id,
        guide_name=(guide_name or "").strip() or stem,
        file_type=extension.lstrip("."),
        file_size_bytes=len(contents),
        source_filename=file.filename,
        upload_path=str(save_path),
    )
    return service.ingest(store, settings, record)


@router.get("")
async def list_guides(
    request: Request,
    guide_id: Optional[str] = None,
    status: Optional[str] = None,
    settings: Settings = Depends(get_settings),
):
    return _get_store(request, settings).get_records(guide_id=guide_id, status=status)


@router.get("/active/rules")
async def get_active_rules(request: Request, settings: Settings = Depends(get_settings)):
    """The most recently approved guide version. A migration uses a GWP only when it names one."""
    rule_set = service.load_active_rule_set(_get_store(request, settings))
    if rule_set is None:
        raise NotFoundError("No approved GWP guide yet", code="GWP_NO_ACTIVE_GUIDE")
    return rule_set.to_clean_dict()


@router.get("/{guide}")
async def get_guide(request: Request, guide: str, version: Optional[int] = None, settings: Settings = Depends(get_settings)):
    return _resolve(_get_store(request, settings), guide, version)


@router.get("/{guide}/units")
async def get_guide_units(request: Request, guide: str, version: Optional[int] = None, settings: Settings = Depends(get_settings)):
    record = _resolve(_get_store(request, settings), guide, version)
    if not record.get("units_path") or not Path(record["units_path"]).exists():
        raise NotFoundError("Guide has not been parsed", code="GWP_UNITS_NOT_FOUND")
    return service.load_units(Path(record["units_path"])).to_clean_dict()


@router.post("/{guide}/extract")
async def extract_rules(request: Request, guide: str, version: Optional[int] = None, settings: Settings = Depends(get_settings)):
    """Run the LLM rule extraction. Replaces any earlier candidates for this version."""
    store = _get_store(request, settings)
    record = _resolve(store, guide, version)
    chain_factory = getattr(request.app.state, "chain_factory", None)
    if chain_factory is None:
        from app.services.llm.chain_factory import ChainFactory

        chain_factory = ChainFactory(settings)
    extractor = GwpRuleExtractor.from_factory(chain_factory)
    return await service.extract(store, settings, record, extractor)


@router.get("/{guide}/rules")
async def get_rules(request: Request, guide: str, version: Optional[int] = None, settings: Settings = Depends(get_settings)):
    record = _resolve(_get_store(request, settings), guide, version)
    return service.load_rule_set(_rules_path(record)).to_clean_dict()


@router.put("/{guide}/rules")
async def put_rules(
    request: Request,
    guide: str,
    rule_set: GwpRuleSet,
    version: Optional[int] = None,
    settings: Settings = Depends(get_settings),
):
    """Save a reviewed rule set (edit, add, reject rules). Also imports a hand-curated rules file."""
    store = _get_store(request, settings)
    record = _resolve(store, guide, version)
    try:
        record = service.save_reviewed(store, settings, record, rule_set)
    except service.RuleSetInvalid as exc:
        raise _invalid(exc.problems) from exc
    return record


@router.post("/{guide}/approve")
async def approve(
    request: Request,
    guide: str,
    body: Optional[ApproveRequest] = None,
    version: Optional[int] = None,
    settings: Settings = Depends(get_settings),
):
    """Approve the named rules, or every remaining candidate when no IDs are given."""
    store = _get_store(request, settings)
    record = _resolve(store, guide, version)
    rule_set = service.load_rule_set(_rules_path(record))
    updated, unknown = service.approve_rules(rule_set, body.rule_ids if body else None)
    if unknown:
        raise _invalid([f"unknown rule_id {rule_id!r}" for rule_id in unknown])
    try:
        record = service.save_reviewed(store, settings, record, updated)
    except service.RuleSetInvalid as exc:
        raise _invalid(exc.problems) from exc
    return record


@router.get("/{guide}/report")
async def get_report(request: Request, guide: str, version: Optional[int] = None, settings: Settings = Depends(get_settings)):
    record = _resolve(_get_store(request, settings), guide, version)
    if not record.get("report_path") or not Path(record["report_path"]).exists():
        raise NotFoundError("No extraction report for this guide version", code="GWP_REPORT_NOT_FOUND")
    return service.load_report(Path(record["report_path"])).to_clean_dict()


@router.delete("/{guide}")
async def delete_guide(request: Request, guide: str, version: Optional[int] = None, settings: Settings = Depends(get_settings)):
    store = _get_store(request, settings)
    record = _resolve(store, guide, version)
    store.delete_record(record["id"])
    return {"deleted": True, "guide_id": record["guide_id"], "version": record["version"]}

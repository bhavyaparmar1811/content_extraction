"""Run the finished migration stages on one SOP, exactly as a job would, and collect what they produced.

Uses throwaway stores in a work folder and the production stage functions
(``stages.parsing``, ``stages.plan_sections``). The template readiness gate
is not applied, so a template that is not ready yet can still be inspected;
the report says that a real job would refuse it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from app.config.settings import Settings, get_settings
from app.schemas.v2 import (
    ArtifactKind,
    Claim,
    GwpRuleSet,
    JobMode,
    MigrationJob,
    ProtectedFacts,
    QualityReport,
    SectionDraft,
    SectionPlan,
    SlotDraft,
    SourceDocument,
    TemplateModel,
    ValidationIssue,
)
from app.stores.gwp_store import GwpStore
from app.stores.migration_store import MigrationStore
from app.stores.sop_store import SopStore
from app.stores.template_store import TemplateStore

from ..artifacts import ArtifactWriter
from ..gwp.ingest import parse_document
from ..gwp.service import load_rule_set, save_rule_set
from ..inputs import InputResolver
from ..quality.preservation import check_preservation
from ..stages import StageContext, parsing, plan_sections
from ..template.overrides import TemplateConfig, load_config
from ..template.readiness import ReadinessReport
from ..template.service import build_template_model, normalize_and_save
from .checks import Check, evaluate
from .completeness import Completeness, check_completeness
from .golden import find_golden, golden_problems, missing_must_preserve


@dataclass
class TemplateInfo:
    path: Path
    model: TemplateModel
    readiness: ReadinessReport
    normalized: bool
    config_path: Optional[Path] = None
    error: Optional[str] = None


@dataclass
class Inspection:
    sop_path: Path
    doc_id: str
    template: TemplateInfo
    source: SourceDocument
    rules: GwpRuleSet
    facts: ProtectedFacts
    plan: SectionPlan
    plan_report: QualityReport
    completeness: Completeness
    self_check: list[ValidationIssue]
    events: list[dict]
    golden: Optional[dict] = None
    golden_problems: list[str] = field(default_factory=list)
    missing_preserve: list[str] = field(default_factory=list)
    artifacts: dict[str, Path] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    llm: bool = False


def safe_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")[:60] or "SOP"


def _settings(root: Path) -> Settings:
    settings = Settings(project_root=root)
    settings.resolve_paths(root)
    settings.ensure_directories()
    return settings


def prepare_template(path: Path, config_path: Optional[Path], workdir: Path) -> TemplateInfo:
    """Normalize the template with its config, as the template API does; fall back to detection only."""
    config = load_config(config_path) if config_path and Path(config_path).exists() else TemplateConfig()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        out = normalize_and_save(path, "TPL", 1, workdir, config)
        return TemplateInfo(Path(path), out.build.model, out.build.report, True, config_path)
    except Exception as exc:  # a template the normalizer cannot handle is still worth inspecting
        logger.warning(f"Template normalization failed, inspecting the detected model: {exc}")
        build = build_template_model(path, "TPL", 1, config)
        return TemplateInfo(Path(path), build.model, build.report, False, config_path, f"normalization failed: {exc}")


async def inspect_sop(
    sop_path: Path,
    template: TemplateInfo,
    out_dir: Path,
    gwp_rules_path: Optional[Path] = None,
    golden_dir: Optional[Path] = None,
    llm: bool = False,
) -> Inspection:
    sop_path = Path(sop_path)
    doc_id = safe_id(sop_path.stem)
    # Short internal names: Windows paths stop at 260 characters and SOP file names can be long.
    short = hashlib.sha1(sop_path.name.encode("utf-8")).hexdigest()[:8]
    work = Path(out_dir) / "_work" / f"{short}-{datetime.now():%Y%m%d%H%M%S%f}"
    settings = _settings(work)
    settings.section_planner_llm = "confirm" if llm else "off"

    source = parse_document(sop_path, settings, doc_id)
    units_file = work / "units.json"
    units_file.write_text(json.dumps(source.to_clean_dict(), ensure_ascii=False), encoding="utf-8")

    sop_store = SopStore(work / "sop.db", settings)
    sop = sop_store.upsert_record(job_id="inspect", document_uid=doc_id, units_path=str(units_file))
    template_store = TemplateStore(work / "templates.db", settings)
    model_file = work / "template_model.json"
    model_file.write_text(json.dumps(template.model.to_clean_dict(), ensure_ascii=False), encoding="utf-8")
    record = template_store.upsert_record(template_uid="TPL", template_name=template.path.stem)
    template_store.update_template_model(record["id"], str(model_file), None, template.readiness.status.value)

    gwp_store = GwpStore(work / "gwp.db", settings)
    gwp_id = gwp_version = None
    if gwp_rules_path:
        rules_in = load_rule_set(Path(gwp_rules_path))
        guide = gwp_store.create_record(rules_in.guide_id, rules_in.guide_id, "json")
        rules_file = work / "gwp_rules.json"
        save_rule_set(rules_file, rules_in.model_copy(update={"version": guide["version"]}))
        gwp_store.update_fields(guide["id"], rules_path=str(rules_file), status="approved")
        gwp_id, gwp_version = guide["guide_id"], guide["version"]

    store = MigrationStore(work / "migrations.db", settings)
    writer = ArtifactWriter(work / "artifacts", store)
    job = store.create_job(MigrationJob(
        job_id=f"INSPECT-{short}", sop_record_id=sop["id"], template_id="TPL", template_version=1,
        gwp_id=gwp_id, gwp_version=gwp_version, mode=JobMode.AUTO, created_by="inspect",
    ))
    chain_factory = None
    if llm:
        from app.services.llm.chain_factory import ChainFactory

        chain_factory = ChainFactory(get_settings())
    ctx = StageContext(job=job, store=store, artifacts=writer, inputs=InputResolver(sop_store, template_store, gwp_store),
                       settings=settings, chain_factory=chain_factory)
    await parsing(ctx)
    ctx.job = store.get_job(job.job_id)
    await plan_sections(ctx)
    job = store.get_job(job.job_id)

    facts = writer.latest(job, ArtifactKind.PROTECTED_FACTS, ProtectedFacts)
    insp = Inspection(
        sop_path=sop_path,
        doc_id=doc_id,
        template=template,
        source=writer.latest(job, ArtifactKind.SOURCE_MODEL, SourceDocument),
        rules=writer.latest(job, ArtifactKind.GWP_RULES, GwpRuleSet),
        facts=facts,
        plan=writer.latest(job, ArtifactKind.SECTION_PLAN, SectionPlan),
        plan_report=writer.latest(job, ArtifactKind.QUALITY_REPORT, QualityReport, scope="section_plan"),
        completeness=check_completeness(sop_path, source),
        self_check=_self_check(source, facts),
        events=store.list_events(job.job_id),
        llm=llm,
    )
    insp.golden = find_golden(golden_dir, sop_path.name) if golden_dir else None
    if insp.golden:
        insp.golden_problems = golden_problems(insp.plan, insp.source, template.model, insp.golden)
        insp.missing_preserve = missing_must_preserve(insp.source, insp.golden)
    insp.checks = evaluate(insp)

    dest = Path(out_dir) / doc_id
    dest.mkdir(parents=True, exist_ok=True)
    for ref in job.artifacts:
        name = Path(ref.path).name
        shutil.copy(ref.path, dest / name)
        insp.artifacts[name] = dest / name
    return insp


def _self_check(source: SourceDocument, facts: ProtectedFacts) -> list[ValidationIssue]:
    """Claims identical to the passages must raise no preservation issue; anything else is an extractor bug."""
    units = [u for u in source.iter_units() if not u.is_boilerplate and u.text.strip()]
    draft = SectionDraft(target_section_id="SELF", version=1, slots=[SlotDraft(slot_id="SELF", claims=[
        Claim(claim_id=f"C-{i:04d}", text=u.text, source_unit_ids=[u.unit_id]) for i, u in enumerate(units)
    ])])
    return check_preservation(facts, source, [draft])

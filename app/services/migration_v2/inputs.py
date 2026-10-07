"""Resolve and load a migration's inputs: SOP source units and template model (required),
and a GWP guide (optional).

A GWP is used only when the job names one. Without it the job still runs:
its rule set holds just the built-in preservation rules (numbers, dates,
obligations...), which protect the source content and are not writing style.

``resolve`` runs when a job is created and refuses inputs that cannot work
(no units file, template not ``ready``, a named GWP that is not approved).
The PARSING stage then loads them and snapshots them as job artifacts, so
later edits to the SOP, template or guide never change a running job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.schemas.v2 import GwpRuleSet, MigrationJob, SourceDocument, TemplateModel
from app.services.migration_v2.gwp.baseline import baseline_rules
from app.services.migration_v2.gwp.service import load_rule_set


class InputError(Exception):
    """An input is missing or not usable. ``status`` is the HTTP status the API should return."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedInputs:
    sop_record_id: int
    template_id: str
    template_version: int
    gwp_id: Optional[str]
    gwp_version: Optional[int]


class InputResolver:
    def __init__(self, sop_store: Any, template_store: Any, gwp_store: Any = None):
        self.sop_store = sop_store
        self.template_store = template_store
        self.gwp_store = gwp_store

    # ── Records ───────────────────────────────────────────────────────

    def _sop_record(self, sop_record_id: int) -> dict:
        record = self.sop_store.get_record_by_id(sop_record_id)
        if record is None:
            raise InputError(404, "SOP_NOT_FOUND", f"SOP record {sop_record_id} not found")
        return record

    def _template_record(self, template_id: str, version: Optional[int]) -> dict:
        records = self.template_store.get_records(template_uid=template_id)
        if not records and str(template_id).isdigit():
            by_id = self.template_store.get_record_by_id(int(template_id))
            records = [by_id] if by_id else []
        if version is not None:
            records = [r for r in records if r["template_version"] == version]
        if not records:
            suffix = f" v{version}" if version is not None else ""
            raise InputError(404, "TEMPLATE_NOT_FOUND", f"Template '{template_id}'{suffix} not found")
        return max(records, key=lambda r: r["template_version"])

    def _gwp_record(self, gwp_id: str, version: Optional[int]) -> dict:
        if self.gwp_store is None:
            raise InputError(404, "GWP_NOT_FOUND", "No GWP store configured")
        records = self.gwp_store.get_records(guide_id=gwp_id)
        if version is not None:
            records = [r for r in records if r["version"] == version]
        if not records:
            raise InputError(404, "GWP_NOT_FOUND", f"GWP guide '{gwp_id}' not found")
        return max(records, key=lambda r: r["version"])

    # ── Creation-time checks ──────────────────────────────────────────

    def resolve(
        self,
        sop_record_id: int,
        template_id: str,
        template_version: Optional[int] = None,
        gwp_id: Optional[str] = None,
        gwp_version: Optional[int] = None,
    ) -> ResolvedInputs:
        sop = self._sop_record(sop_record_id)
        if not sop.get("units_path") or not Path(sop["units_path"]).exists():
            raise InputError(409, "SOP_UNITS_MISSING", "The SOP has no v2 source units; re-run its extraction")

        template = self._template_record(template_id, template_version)
        if template.get("readiness_status") != "ready" or not template.get("model_path"):
            raise InputError(
                409, "TEMPLATE_NOT_READY",
                f"Template '{template['template_uid']}' v{template['template_version']} is "
                f"'{template.get('readiness_status') or 'not assessed'}'; normalize it until readiness is 'ready'",
            )
        if not Path(template["model_path"]).exists():
            raise InputError(409, "TEMPLATE_MODEL_MISSING", "The template model file is missing; normalize it again")

        guide = None  # GWP is optional and opt-in: never picked implicitly
        if gwp_id:
            guide = self._gwp_record(gwp_id, gwp_version)
            if guide["status"] != "approved":
                raise InputError(409, "GWP_NOT_APPROVED", f"GWP guide '{gwp_id}' v{guide['version']} is '{guide['status']}'")

        return ResolvedInputs(
            sop_record_id=sop_record_id,
            template_id=template["template_uid"],
            template_version=template["template_version"],
            gwp_id=guide["guide_id"] if guide else None,
            gwp_version=guide["version"] if guide else None,
        )

    # ── Loading (PARSING stage) ───────────────────────────────────────

    def source_document(self, job: MigrationJob) -> SourceDocument:
        sop = self._sop_record(job.sop_record_id)
        if not sop.get("units_path") or not Path(sop["units_path"]).exists():
            raise InputError(409, "SOP_UNITS_MISSING", "The SOP has no v2 source units")
        return SourceDocument.model_validate_json(Path(sop["units_path"]).read_text(encoding="utf-8"))

    def template_model(self, job: MigrationJob) -> TemplateModel:
        template = self._template_record(job.template_id, job.template_version)
        if not template.get("model_path") or not Path(template["model_path"]).exists():
            raise InputError(409, "TEMPLATE_MODEL_MISSING", "The template model file is missing")
        return TemplateModel.model_validate_json(Path(template["model_path"]).read_text(encoding="utf-8"))

    def gwp_rules(self, job: MigrationJob) -> GwpRuleSet:
        if not job.gwp_id:
            return GwpRuleSet(guide_id="BASELINE", version=1, rules=baseline_rules())
        guide = self._gwp_record(job.gwp_id, job.gwp_version)
        if not guide.get("rules_path") or not Path(guide["rules_path"]).exists():
            raise InputError(409, "GWP_RULES_MISSING", f"GWP guide '{job.gwp_id}' has no rules file")
        return load_rule_set(Path(guide["rules_path"]))

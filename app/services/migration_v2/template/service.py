"""Template model pipeline: detect → config overrides → (normalize) → readiness (Phase 3).

Files written for template ``uid`` version ``n`` (in ``template_output_dir``):

- ``{uid}_v{n}_normalized.docx``: the template with tagged content controls;
- ``{uid}_v{n}_model.json``: the ``TemplateModel``;
- ``{uid}_v{n}_readiness.json``: the ``ReadinessReport``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.schemas.v2 import TemplateModel

from .normalizer import NormalizationResult, normalize_template
from .overrides import TemplateConfig, apply_slot_overrides
from .readiness import ReadinessReport, assess
from .slot_detector import DetectionResult, detect_template_model


@dataclass
class TemplateBuild:
    detection: DetectionResult
    report: ReadinessReport
    unknown_config_refs: list[str]

    @property
    def model(self) -> TemplateModel:
        return self.detection.model


@dataclass
class NormalizeOutput:
    build: TemplateBuild
    normalization: NormalizationResult
    normalized_path: Path
    model_path: Path
    report_path: Path


def output_paths(out_dir: Path, template_uid: str, version: int) -> tuple[Path, Path, Path]:
    stem = Path(out_dir) / f"{template_uid}_v{version}"
    return (stem.with_name(stem.name + "_normalized.docx"),
            stem.with_name(stem.name + "_model.json"),
            stem.with_name(stem.name + "_readiness.json"))


def build_template_model(
    docx_path: str | Path,
    template_id: str,
    template_version: int = 1,
    config: Optional[TemplateConfig] = None,
    v1_output: Optional[dict] = None,
) -> TemplateBuild:
    """Detect slots, apply the config and assess readiness. Writes nothing."""
    config = config or TemplateConfig()
    detection = detect_template_model(docx_path, template_id, template_version, config)
    unknown = apply_slot_overrides(detection.model, config)
    if v1_output:
        link_instruction_ids(detection.model, v1_output)
    report = assess(detection, unknown)
    detection.model.readiness = report.status
    # Re-validate: a "ready" model must satisfy the contract's own checks.
    detection.model = TemplateModel.model_validate(detection.model.model_dump())
    return TemplateBuild(detection, report, unknown)


def normalize_and_save(
    source_path: str | Path,
    template_id: str,
    template_version: int,
    out_dir: Path,
    config: Optional[TemplateConfig] = None,
    v1_output: Optional[dict] = None,
) -> NormalizeOutput:
    """Normalize the template, then rebuild the model from the normalized copy and save everything."""
    config = config or TemplateConfig()
    normalized_path, model_path, report_path = output_paths(out_dir, template_id, template_version)
    first = detect_template_model(source_path, template_id, template_version, config)
    normalization = normalize_template(first, normalized_path)
    build = rebuild_from_normalized(source_path, normalized_path, template_id, template_version, config, v1_output)
    save_build(build, model_path, report_path)
    return NormalizeOutput(build, normalization, normalized_path, model_path, report_path)


def rebuild_from_normalized(
    source_path: str | Path,
    normalized_path: str | Path,
    template_id: str,
    template_version: int,
    config: Optional[TemplateConfig] = None,
    v1_output: Optional[dict] = None,
) -> TemplateBuild:
    build = build_template_model(normalized_path, template_id, template_version, config, v1_output)
    build.model.source_file = str(source_path)
    build.model.normalized_file = str(normalized_path)
    return build


def save_build(build: TemplateBuild, model_path: Path, report_path: Path) -> None:
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(model_path).write_text(json.dumps(build.model.to_clean_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    Path(report_path).write_text(json.dumps(build.report.to_clean_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def link_instruction_ids(model: TemplateModel, v1_output: dict) -> None:
    """Fill ``TargetSlot.instruction_ids`` with the v2.0 extraction's instruction IDs.

    A v1 instruction belongs to a slot when its text is part of the slot's
    instruction and it comes from the same section.
    """
    by_title = {_norm(s.get("title")): s for s in v1_output.get("sections", [])}
    for section in model.sections:
        v1_section = by_title.get(_norm(section.heading)) or next(
            (s for k, s in by_title.items() if _norm(section.heading) in k), None
        )
        if v1_section is None:
            continue
        instructions = [(i.get("instruction_id"), _norm(i.get("text"))) for i in v1_section.get("authoring_instructions", [])]
        for slot in section.slots:
            slot_text = _norm(slot.instruction)
            slot.instruction_ids = [iid for iid, text in instructions if iid and text and text in slot_text]

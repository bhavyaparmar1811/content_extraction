"""GWP guide lifecycle: parse, extract candidates, human review, approval, selection.

Files per guide version, in ``settings.gwp_dir``:
    {guide_id}_v{n}_units.json    SourceDocument of the guide (what rules cite)
    {guide_id}_v{n}_rules.json    GwpRuleSet, the source of truth after review
    {guide_id}_v{n}_report.json   GwpExtractionReport (coverage and corrections)

Status flow: uploaded -> parsed -> extracting -> in_review -> approved.
A guide is ``approved`` once no rule is left as a candidate. Saving rules
with candidates in them moves it back to ``in_review``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config.settings import Settings
from app.schemas.v2 import (
    ContentType,
    GwpExtractionReport,
    GwpRule,
    GwpRuleSet,
    RuleCategory,
    RuleOrigin,
    RuleStatus,
    SourceDocument,
)
from app.stores.gwp_store import GwpStore

from .baseline import BASELINE_IDS, baseline_rules
from .checks import check_params_problems
from .extractor import GwpRuleExtractor
from .ingest import parse_guide, rule_input_sections


class RuleSetInvalid(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class GuidePaths:
    units: Path
    rules: Path
    report: Path


def guide_paths(gwp_dir: Path, guide_id: str, version: int) -> GuidePaths:
    stem = f"{guide_id}_v{version}"
    return GuidePaths(
        units=gwp_dir / f"{stem}_units.json",
        rules=gwp_dir / f"{stem}_rules.json",
        report=gwp_dir / f"{stem}_report.json",
    )


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_units(path: Path) -> SourceDocument:
    return SourceDocument.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_rule_set(path: Path) -> GwpRuleSet:
    return GwpRuleSet.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_rule_set(path: Path, rule_set: GwpRuleSet) -> None:
    _write_json(path, rule_set.to_clean_dict())


def load_report(path: Path) -> GwpExtractionReport:
    return GwpExtractionReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ── Validation of reviewed rule sets ──────────────────────────────────


def validate_rule_set(
    rule_set: GwpRuleSet,
    guide_id: str,
    version: int,
    units: Optional[SourceDocument] = None,
) -> list[str]:
    """Problems that block saving a reviewed rule set; empty when it is valid."""
    problems: list[str] = []
    if rule_set.guide_id != guide_id or rule_set.version != version:
        problems.append(
            f"rule set is for {rule_set.guide_id} v{rule_set.version}, expected {guide_id} v{version}"
        )
    known = {u.unit_id for u in units.iter_units()} if units is not None else None
    for rule in rule_set.rules:
        if rule.origin == RuleOrigin.BASELINE and rule.rule_id not in BASELINE_IDS:
            problems.append(f"{rule.rule_id}: only built-in rules may have origin 'baseline'")
        if rule.origin == RuleOrigin.GUIDE:
            if not rule.source_unit_ids:
                problems.append(f"{rule.rule_id}: a guide rule must cite source units (or use origin 'manual')")
            elif known is not None:
                missing = [i for i in rule.source_unit_ids if i not in known]
                if missing:
                    problems.append(f"{rule.rule_id}: unknown source units {missing}")
        problems += [f"{rule.rule_id}: {p}" for p in check_params_problems(rule)]
    return problems


def with_baseline(rule_set: GwpRuleSet) -> GwpRuleSet:
    """Re-add any built-in rule a reviewer deleted. Baseline rules may be edited, not removed."""
    present = {r.rule_id for r in rule_set.rules}
    missing = [r for r in baseline_rules() if r.rule_id not in present]
    if not missing:
        return rule_set
    return rule_set.model_copy(update={"rules": missing + list(rule_set.rules)})


def approve_rules(rule_set: GwpRuleSet, rule_ids: Optional[Iterable[str]] = None) -> tuple[GwpRuleSet, list[str]]:
    """Approve the given candidates (all candidates when ``rule_ids`` is None).

    Returns the updated set and the IDs that were not found. Rejected rules
    stay rejected unless named explicitly.
    """
    wanted = None if rule_ids is None else set(rule_ids)
    known = {r.rule_id for r in rule_set.rules}
    unknown = sorted(wanted - known) if wanted is not None else []
    rules = []
    for rule in rule_set.rules:
        if wanted is None:
            change = rule.status == RuleStatus.CANDIDATE
        else:
            change = rule.rule_id in wanted
        rules.append(rule.model_copy(update={"status": RuleStatus.APPROVED}) if change else rule)
    return rule_set.model_copy(update={"rules": rules}), unknown


def rule_counts(rule_set: GwpRuleSet) -> dict[str, int]:
    return {
        "total_rules": len(rule_set.rules),
        "approved_rules": sum(r.status == RuleStatus.APPROVED for r in rule_set.rules),
        "candidate_rules": sum(r.status == RuleStatus.CANDIDATE for r in rule_set.rules),
    }


def review_status(rule_set: GwpRuleSet) -> str:
    return "in_review" if rule_counts(rule_set)["candidate_rules"] else "approved"


# ── Lifecycle steps (store + files) ───────────────────────────────────


def ingest(store: GwpStore, settings: Settings, record: dict[str, Any]) -> dict[str, Any]:
    """Parse the uploaded guide into source units."""
    paths = guide_paths(settings.gwp_dir, record["guide_id"], record["version"])
    try:
        doc = parse_guide(Path(record["upload_path"]), settings, record["guide_id"])
    except Exception as exc:
        store.update_fields(record["id"], status="failed", error=f"parse failed: {exc}")
        raise
    _write_json(paths.units, doc.to_clean_dict())
    input_units = sum(len(units) for _, units in rule_input_sections(doc))
    return store.update_fields(
        record["id"], units_path=str(paths.units), total_units=input_units, status="parsed", error=None
    )


async def extract(store: GwpStore, settings: Settings, record: dict[str, Any], extractor: GwpRuleExtractor) -> dict[str, Any]:
    """Run the LLM extractor and save candidates plus the review report."""
    if not record.get("units_path"):
        record = ingest(store, settings, record)
    doc = load_units(Path(record["units_path"]))
    paths = guide_paths(settings.gwp_dir, record["guide_id"], record["version"])
    store.update_fields(record["id"], status="extracting", error=None)
    try:
        rule_set, report = await extractor.extract(doc, record["guide_id"], record["version"], record["guide_name"])
    except Exception as exc:
        store.update_fields(record["id"], status="failed", error=f"rule extraction failed: {exc}")
        raise
    save_rule_set(paths.rules, rule_set)
    _write_json(paths.report, report.to_clean_dict())
    return store.update_fields(
        record["id"],
        rules_path=str(paths.rules),
        report_path=str(paths.report),
        prompt_version=report.prompt_version,
        model=report.model,
        status=review_status(rule_set),
        **rule_counts(rule_set),
    )


def save_reviewed(store: GwpStore, settings: Settings, record: dict[str, Any], rule_set: GwpRuleSet) -> dict[str, Any]:
    """Validate and save a human-edited rule set (also used to import a hand-curated file)."""
    rule_set = with_baseline(rule_set)
    units = load_units(Path(record["units_path"])) if record.get("units_path") else None
    problems = validate_rule_set(rule_set, record["guide_id"], record["version"], units)
    if problems:
        raise RuleSetInvalid(problems)
    paths = guide_paths(settings.gwp_dir, record["guide_id"], record["version"])
    save_rule_set(paths.rules, rule_set)
    return store.update_fields(
        record["id"], rules_path=str(paths.rules), status=review_status(rule_set), **rule_counts(rule_set)
    )


# ── Selection for later phases ────────────────────────────────────────


def load_active_rule_set(store: GwpStore) -> Optional[GwpRuleSet]:
    record = store.get_active()
    if not record or not record.get("rules_path"):
        return None
    return load_rule_set(Path(record["rules_path"]))


def select_rules(
    rule_set: Optional[GwpRuleSet],
    categories: Iterable[RuleCategory],
    content_types: Optional[Iterable[ContentType]] = None,
) -> list[GwpRule]:
    """Approved rules of the given categories for the given content types.

    With no guide (GWP is optional), only the built-in preservation rules apply.
    """
    if rule_set is None:
        rule_set = GwpRuleSet(guide_id="BASELINE", version=1, rules=baseline_rules())
    return rule_set.select(list(categories), list(content_types) if content_types else None)

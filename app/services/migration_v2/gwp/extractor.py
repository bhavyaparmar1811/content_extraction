"""LLM pass that turns a parsed GWP guide into a candidate ``GwpRuleSet``.

The model proposes rules per batch of sections. Everything after that is
deterministic: citations are checked against the input units, invalid
deterministic params are downgraded to ``semantic``, duplicates are merged,
IDs are assigned per category in guide order, and the built-in baseline
preservation rules are added. Every correction is listed in the
``GwpExtractionReport`` for the reviewer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.schemas.v2 import (
    ContentType,
    DroppedCandidate,
    GwpExtractionReport,
    GwpRule,
    GwpRuleSet,
    RuleCategory,
    RuleCheck,
    RuleOrigin,
    RuleStatus,
    Severity,
    SkippedGuidance,
    SourceDocument,
    SourceUnit,
)
from app.services.llm.prompts.v2.gwp_extractor import (
    GWP_EXTRACTOR_SYSTEM_PROMPT,
    GWP_EXTRACTOR_USER_PROMPT,
    PROMPT_VERSION,
)

from .baseline import BASELINE_IDS, baseline_rules
from .checks import check_params_problems, describe_check_kinds
from .ingest import render_section, rule_input_sections

# Characters of rendered guide text per LLM call. The sample guide (~25k chars) takes two calls.
DEFAULT_BATCH_CHARS = 16_000


# ── LLM output schema ─────────────────────────────────────────────────
# Kept separate from GwpRule: the model does not choose IDs, status or origin,
# and params travel as a JSON string because strict structured output rejects
# free-form objects.


class CandidateRule(BaseModel):
    category: RuleCategory
    text: str
    applies_to_content_types: list[ContentType] = Field(default_factory=list)
    severity: Severity = Severity.MEDIUM
    check: RuleCheck = RuleCheck.SEMANTIC
    params_json: str = Field(default="{}", description="JSON object with the rule parameters")
    source_unit_ids: list[str] = Field(default_factory=list)


class CandidateSkip(BaseModel):
    source_unit_ids: list[str] = Field(default_factory=list)
    reason: str


class GwpExtractionOutput(BaseModel):
    rules: list[CandidateRule] = Field(default_factory=list)
    skipped: list[CandidateSkip] = Field(default_factory=list)


# ── Batching ──────────────────────────────────────────────────────────


@dataclass
class _Batch:
    unit_ids: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    chars: int = 0


def build_batches(doc: SourceDocument, max_chars: int = DEFAULT_BATCH_CHARS) -> list[_Batch]:
    """Whole sections per batch, in order; a section larger than the budget gets its own batch."""
    batches: list[_Batch] = []
    current = _Batch()
    for section, units in rule_input_sections(doc):
        text = render_section(section, units)
        if current.parts and current.chars + len(text) > max_chars:
            batches.append(current)
            current = _Batch()
        current.parts.append(text)
        current.unit_ids.extend(u.unit_id for u in units)
        current.chars += len(text)
    if current.parts:
        batches.append(current)
    return batches


# ── Post-processing ───────────────────────────────────────────────────


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _parse_params(raw: str) -> tuple[dict[str, Any], Optional[str]]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return {}, f"params_json is not valid JSON ({exc.msg})"
    if not isinstance(value, dict):
        return {}, "params_json is not a JSON object"
    return value, None


def build_rule_set(
    doc: SourceDocument,
    outputs: list[GwpExtractionOutput],
    guide_id: str,
    version: int,
    input_unit_ids: list[str],
    model: Optional[str] = None,
) -> tuple[GwpRuleSet, GwpExtractionReport]:
    """Validate and number the model's candidates, then add the baseline rules."""
    known = set(input_unit_ids)
    seq: dict[str, int] = {u.unit_id: u.seq for u in doc.iter_units()}
    report = GwpExtractionReport(
        guide_id=guide_id, version=version, prompt_version=PROMPT_VERSION, model=model,
        input_unit_ids=list(input_unit_ids),
    )

    drafts: list[GwpRule] = []
    by_key: dict[tuple[str, str], GwpRule] = {}
    pending_problems: dict[int, list[str]] = {}

    for output in outputs:
        for skip in output.skipped:
            ids = [i for i in skip.source_unit_ids if i in known]
            if ids:
                report.skipped.append(SkippedGuidance(source_unit_ids=ids, reason=skip.reason))

        for cand in output.rules:
            text = re.sub(r"\s+", " ", cand.text).strip()
            if not text:
                continue
            ids = list(dict.fromkeys(i for i in cand.source_unit_ids if i in known))
            if not ids:
                cited = ", ".join(cand.source_unit_ids) or "none"
                report.dropped.append(DroppedCandidate(text=text, reason=f"no valid source unit (cited: {cited})"))
                continue

            key = (cand.category.value, _norm(text))
            if key in by_key:  # same rule from two batches: merge the citations
                existing = by_key[key]
                existing.source_unit_ids = list(dict.fromkeys(existing.source_unit_ids + ids))
                continue

            params, parse_error = _parse_params(cand.params_json)
            rule = GwpRule(
                rule_id=f"{cand.category.value}-000",  # renumbered below
                category=cand.category,
                text=text,
                applies_to_content_types=cand.applies_to_content_types,
                severity=cand.severity,
                check=cand.check,
                params=params,
                source_unit_ids=ids,
                status=RuleStatus.CANDIDATE,
                origin=RuleOrigin.GUIDE,
            )
            problems = ([parse_error] if parse_error else []) + check_params_problems(rule)
            if problems and rule.check == RuleCheck.DETERMINISTIC:
                rule.check = RuleCheck.SEMANTIC
                pending_problems[id(rule)] = problems
            by_key[key] = rule
            drafts.append(rule)

    # Number each category in guide order (first cited unit); baseline PRES IDs stay reserved.
    drafts.sort(key=lambda r: min(seq.get(i, 10**9) for i in r.source_unit_ids))
    counters: dict[str, int] = {}
    reserved = set(BASELINE_IDS)
    for rule in drafts:
        cat = rule.category.value
        while True:
            counters[cat] = counters.get(cat, 0) + 1
            rule_id = f"{cat}-{counters[cat]:03d}"
            if rule_id not in reserved:
                break
        rule.rule_id = rule_id
        if id(rule) in pending_problems:
            report.downgraded[rule_id] = pending_problems[id(rule)]

    cited = {i for r in drafts for i in r.source_unit_ids} | {i for s in report.skipped for i in s.source_unit_ids}
    report.uncovered_unit_ids = [i for i in input_unit_ids if i not in cited]

    rules = baseline_rules() + drafts
    rule_set = GwpRuleSet(guide_id=guide_id, version=version, source_file=doc.source_file, rules=rules)
    return rule_set, report


# ── Extractor ─────────────────────────────────────────────────────────


class GwpRuleExtractor:
    """Runs the LLM over the guide in batches. ``chain`` is a structured-output runnable."""

    def __init__(self, chain: Any, model_name: Optional[str] = None, max_chars: int = DEFAULT_BATCH_CHARS):
        self.chain = chain
        self.model_name = model_name
        self.max_chars = max_chars

    @classmethod
    def from_factory(cls, chain_factory: Any, **kwargs) -> "GwpRuleExtractor":
        return cls(chain_factory.create_structured_planner(GwpExtractionOutput), model_name=chain_factory.planner_label(), **kwargs)

    @staticmethod
    def system_prompt() -> str:
        return GWP_EXTRACTOR_SYSTEM_PROMPT.format(
            content_types=", ".join(ct.value for ct in ContentType),
            check_kinds=describe_check_kinds(),
        )

    async def extract(self, doc: SourceDocument, guide_id: str, version: int, guide_title: str = "") -> tuple[GwpRuleSet, GwpExtractionReport]:
        batches = build_batches(doc, self.max_chars)
        system = SystemMessage(content=self.system_prompt())
        outputs: list[GwpExtractionOutput] = []
        for index, batch in enumerate(batches, start=1):
            human = HumanMessage(content=GWP_EXTRACTOR_USER_PROMPT.format(
                guide_title=guide_title or doc.source_file,
                batch_index=index,
                batch_count=len(batches),
                units="\n\n".join(batch.parts),
            ))
            logger.info(f"GWP rule extraction {guide_id} v{version}: batch {index}/{len(batches)} ({batch.chars} chars)")
            result = await self.chain.ainvoke([system, human])
            outputs.append(result if isinstance(result, GwpExtractionOutput) else GwpExtractionOutput.model_validate(result))
        input_ids = [i for b in batches for i in b.unit_ids]
        return build_rule_set(doc, outputs, guide_id, version, input_ids, model=self.model_name)

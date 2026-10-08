"""LLM pass that turns a parsed GWP guide into a candidate ``GwpRuleSet``.

The model proposes rules per batch of sections, then once more for the units
the first pass neither cited nor skipped (coverage pass). Everything after that is
deterministic: citations are checked against the input units, invalid
deterministic params (malformed, or not grounded in the cited guide text) are
downgraded to ``semantic``, duplicates are merged,
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
    CalloutKind,
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
    GWP_COVERAGE_USER_PROMPT,
    GWP_EXTRACTOR_SYSTEM_PROMPT,
    GWP_EXTRACTOR_USER_PROMPT,
    PROMPT_VERSION,
)

from .baseline import BASELINE_IDS, baseline_rules
from .checks import check_params_problems, describe_check_kinds, grounding_problems
from .ingest import render_section, rule_input_sections

# Characters of rendered guide text per LLM call. Larger batches made gpt-4o skim: on the sample guide
# (~20k chars) two 16k batches gave 12 rules and left 54 of 119 units uncovered.
DEFAULT_BATCH_CHARS = 6_000
_SEP = "\n\n"


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


_DEDUP_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "with", "use", "by", "as", "be", "is", "are",
               "that", "this", "it", "its", "on", "at", "from", "your", "you", "ensure", "document", "documents"}
DUPLICATE_SIMILARITY = 0.6


def _sig_words(text: str) -> set[str]:
    words = _norm(text).split()
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _DEDUP_STOP}


def _check_params(rule: GwpRule) -> dict[str, Any]:
    return {k: v for k, v in (rule.params or {}).items() if k != "note"}


def _same_rule(a: GwpRule, b: GwpRule) -> bool:
    """Two candidates state the same instruction: same deterministic check, or nearly the same words."""
    if (a.check == b.check == RuleCheck.DETERMINISTIC and a.category == b.category
            and _check_params(a) == _check_params(b)):
        return True
    wa, wb = _sig_words(a.text), _sig_words(b.text)
    if not wa or not wb:
        return False
    similar = len(wa & wb) / len(wa | wb) >= DUPLICATE_SIMILARITY
    return similar and (a.category == b.category or bool(set(a.source_unit_ids) & set(b.source_unit_ids)))


_SEVERITY_ORDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


def _merge(kept: GwpRule, twin: GwpRule, twin_check_valid: bool) -> None:
    """Fold a duplicate into the rule already kept: union of citations, the stronger severity, a valid check."""
    kept.source_unit_ids = list(dict.fromkeys(kept.source_unit_ids + twin.source_unit_ids))
    kept.applies_to_content_types = list(dict.fromkeys(kept.applies_to_content_types + twin.applies_to_content_types))
    if _SEVERITY_ORDER.index(twin.severity) > _SEVERITY_ORDER.index(kept.severity):
        kept.severity = twin.severity
    if kept.check != RuleCheck.DETERMINISTIC and twin_check_valid:
        kept.check, kept.params = twin.check, twin.params


_PALETTE_LABELS = (
    (re.compile(r"intro|executive|summary", re.I), CalloutKind.INTRODUCTION),
    (re.compile(r"explan", re.I), CalloutKind.EXPLANATION),
    (re.compile(r"attention|warning|caution", re.I), CalloutKind.ATTENTION),
    (re.compile(r"take\s*-?\s*a?\s*-?way|key", re.I), CalloutKind.KEY_TAKEAWAY),
)


def _normalize_palette(params: dict[str, Any]) -> dict[str, Any]:
    """Map callout labels the guide uses ("Key-take-away", "Executive Summary/Introduction") to callout kinds."""
    colors = params.get("colors") if params.get("kind") == "callout_palette" else None
    if not isinstance(colors, dict):
        return params
    mapped: dict[str, Any] = {}
    for label, value in colors.items():
        kind = next((k.value for rx, k in _PALETTE_LABELS if rx.search(str(label))), str(label))
        mapped[kind] = value
    return {**params, "colors": mapped}


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
    text_of: dict[str, str] = {u.unit_id: u.text for u in doc.iter_units()}
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
            params = _normalize_palette(params)
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
            if not problems:
                problems = grounding_problems(rule, " ".join(text_of.get(i, "") for i in ids))
            if problems and rule.check == RuleCheck.DETERMINISTIC:
                rule.check = RuleCheck.SEMANTIC
                pending_problems[id(rule)] = problems
            kept = next((d for d in drafts if _same_rule(d, rule)), None)
            if kept is not None:  # the guide states it twice (e.g. a summary list and a detailed chapter)
                was_deterministic = kept.check == RuleCheck.DETERMINISTIC
                _merge(kept, rule, rule.check == RuleCheck.DETERMINISTIC and not problems)
                if kept.check == RuleCheck.DETERMINISTIC and not was_deterministic:
                    pending_problems.pop(id(kept), None)  # the duplicate brought a valid check
                report.dropped.append(DroppedCandidate(text=text, reason=f"duplicate, merged into: {kept.text[:120]}"))
                by_key[key] = kept
                continue
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
            callout_kinds=", ".join(k.value for k in CalloutKind),
        )

    async def _call(self, system: SystemMessage, content: str) -> GwpExtractionOutput:
        result = await self.chain.ainvoke([system, HumanMessage(content=content)])
        return result if isinstance(result, GwpExtractionOutput) else GwpExtractionOutput.model_validate(result)

    async def extract(self, doc: SourceDocument, guide_id: str, version: int, guide_title: str = "") -> tuple[GwpRuleSet, GwpExtractionReport]:
        title = guide_title or doc.source_file
        batches = build_batches(doc, self.max_chars)
        system = SystemMessage(content=self.system_prompt())
        outputs: list[GwpExtractionOutput] = []
        for index, batch in enumerate(batches, start=1):
            logger.info(f"GWP rule extraction {guide_id} v{version}: batch {index}/{len(batches)} ({batch.chars} chars)")
            outputs.append(await self._call(system, GWP_EXTRACTOR_USER_PROMPT.format(
                guide_title=title, batch_index=index, batch_count=len(batches), units=_SEP.join(batch.parts),
            )))
        input_ids = [i for b in batches for i in b.unit_ids]
        uncovered = set(input_ids) - _accounted(outputs)
        coverage = build_coverage_batches(doc, uncovered, self.max_chars)
        for index, part in enumerate(coverage, start=1):
            logger.info(f"GWP rule extraction {guide_id} v{version}: coverage pass {index}/{len(coverage)} "
                        f"({len(uncovered)} uncovered units)")
            outputs.append(await self._call(system, GWP_COVERAGE_USER_PROMPT.format(guide_title=title, units=part)))
        return build_rule_set(doc, outputs, guide_id, version, input_ids, model=self.model_name)


def _accounted(outputs: list[GwpExtractionOutput]) -> set[str]:
    """Unit IDs cited by a candidate rule or listed as skipped."""
    ids: set[str] = set()
    for output in outputs:
        ids.update(i for rule in output.rules for i in rule.source_unit_ids)
        ids.update(i for skip in output.skipped for i in skip.source_unit_ids)
    return ids


def build_coverage_batches(doc: SourceDocument, uncovered: set[str], max_chars: int = DEFAULT_BATCH_CHARS) -> list[str]:
    """Sections holding uncovered units, rendered with '>>' on those units and the rest as context."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for section, units in rule_input_sections(doc):
        if not uncovered.intersection(u.unit_id for u in units):
            continue
        text = render_section(section, units, marked=uncovered)
        if current and size + len(text) > max_chars:
            parts.append(_SEP.join(current))
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        parts.append(_SEP.join(current))
    return parts

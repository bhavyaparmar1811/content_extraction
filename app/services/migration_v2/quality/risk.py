"""Deterministic risk classifier: which source passages hold high-risk content (Phase 10, migration_plan.md §21).

A passage is high-risk when it states acceptance criteria, safety warnings, regulatory commitments, approval
responsibilities, deadlines, numeric limits, data-retention periods, escalation conditions or prohibitions.
The tags come from the protected-fact registry (deadlines, durations, percentages, prohibitions) and from cue
phrases; no LLM is involved. The gates use them twice:

- an open issue on a high-risk passage blocks completion (``high_risk_unresolved``) until a reviewer resolves it;
- a reworded high-risk passage is listed for direct review (medium, no gate).
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.schemas.v2 import FactKind, Modality, ProtectedFacts, RiskTag, SourceDocument

from .facts import protected_units

_F = re.IGNORECASE

_CUES: dict[RiskTag, re.Pattern] = {
    RiskTag.ACCEPTANCE_CRITERIA: re.compile(
        r"\bacceptance\s+criteri(?:a|on)\b|\bacceptable\s+(?:limit|range|result)s?\b|\bpass(?:/|\s+or\s+)fail\b"
        r"|\bout[-\s]of[-\s]specification\b|\bwithin\s+(?:the\s+)?specification\b|\bOOS\b", _F),
    RiskTag.SAFETY: re.compile(
        r"\bsafety\b|\bhazard(?:ous|s)?\b|\bdanger(?:ous)?\b|\binjur(?:y|ies)\b|\bPPE\b|\bprotective\s+equipment\b"
        r"|\btoxic\b|\bflammable\b|\bpatient\s+risk\b", _F),
    # A commitment, not a mention: "GxP-relevant systems" names the domain, "in accordance with GMP" commits to it.
    RiskTag.REGULATORY: re.compile(
        r"\bregulatory\s+(?:requirement|commitment|obligation|submission|filing|inspection)s?\b|\b21\s*CFR\b"
        r"|\b(?:health|competent|regulatory)\s+authorit(?:y|ies)\b"
        r"|\b(?:in\s+accordance\s+with|according\s+to|in\s+compliance\s+with|compl(?:y|ies)\s+with|required\s+by)\s+"
        r"(?:the\s+)?(?:applicable\s+|relevant\s+|current\s+)?(?:regulations?|laws?|legislation|(?:EU\s+)?GMP|GxP|GDP|GCP|GLP"
        r"|FDA|EMA|MHRA|ICH)\b", _F),
    RiskTag.APPROVAL: re.compile(
        r"\bapprov(?:e|es|ed|al|ing)\b|\bsign[-\s]?off\b|\bauthori[sz](?:e|es|ed|ation)\b|\brelease\s+decision\b", _F),
    RiskTag.DEADLINE: re.compile(
        r"\bwithin\s+\S+\s+(?:working\s+|business\s+|calendar\s+)?(?:hours?|days?|weeks?|months?|years?)\b"
        r"|\bno\s+later\s+than\b|\bat\s+the\s+latest\b|\bdeadline\b|\bdue\s+date\b|\bprior\s+to\s+(?:release|use|start)\b", _F),
    RiskTag.NUMERIC_LIMIT: re.compile(
        r"(?:\b(?:at\s+least|at\s+most|maximum(?:\s+of)?|minimum(?:\s+of)?|not\s+(?:exceed|more\s+than|less\s+than)"
        r"|more\s+than|less\s+than|up\s+to|limit\s+of)\b|[<>≤≥±])\s*\d", _F),
    RiskTag.RETENTION: re.compile(
        r"\bretain(?:ed|s)?\b|\bretention\b|\barchiv(?:e|ed|ing)\b|\bkept\s+for\b|\bstored\s+for\b|\bdestroy(?:ed)?\b", _F),
    RiskTag.ESCALATION: re.compile(
        r"\bescalat(?:e|es|ed|ion)\b|\bnotif(?:y|ies|ied|ication)\b[^.]{0,40}\bimmediately\b|\bimmediately\s+(?:inform|notify|report)\b"
        r"|\breport(?:ed)?\s+to\s+(?:the\s+)?(?:QA|quality|management|authorit)", _F),
}

_FACT_TAGS = {
    FactKind.DEADLINE: RiskTag.DEADLINE,
    FactKind.DURATION: RiskTag.DEADLINE,
    FactKind.DATE: RiskTag.DEADLINE,
    FactKind.PERCENTAGE: RiskTag.NUMERIC_LIMIT,
}


def classify_text(text: str) -> set[RiskTag]:
    return {tag for tag, pattern in _CUES.items() if pattern.search(text or "")}


def high_risk_units(source: SourceDocument, facts: ProtectedFacts) -> dict[str, list[RiskTag]]:
    """unit_id → its risk tags, for the passages that migrate (boilerplate and the TOC are left out)."""
    tags: dict[str, set[RiskTag]] = defaultdict(set)
    units = {u.unit_id for u in protected_units(source)}
    for unit in protected_units(source):
        tags[unit.unit_id] |= classify_text(unit.text)
    for value in facts.values:
        if value.unit_id in units and value.kind in _FACT_TAGS:
            tags[value.unit_id].add(_FACT_TAGS[value.kind])
    for obligation in facts.obligations:
        if obligation.unit_id in units and obligation.modality == Modality.PROHIBITION:
            tags[obligation.unit_id].add(RiskTag.PROHIBITION)
    order = list(RiskTag)
    return {u: sorted(t, key=order.index) for u, t in tags.items() if t}

"""Deterministic GWP style checks on drafted claims (Phase 10).

Run only the job's own approved STY rules whose ``check`` is ``deterministic`` (``gwp/checks.py``
vocabulary), and only on slots the slot plan rewrites. They are soft measures: findings are low-severity
issues and ``soft_scores``, never gates, and they never trigger a repair.

    max_sentence_words  per claim: sentences longer than the limit
    forbidden_terms     per claim: a forbidden word or phrase; a modal word the cited source also states is
                        allowed, because PRES-004 (keep obligation strength) wins over a style rule
    passive_ratio       per section: share of sentences in the passive voice
    readability         per section: Flesch Reading Ease and Flesch-Kincaid grade (syllables estimated)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.v2 import (
    Claim,
    ClaimKind,
    GwpRule,
    IssueCategory,
    IssueSource,
    RuleCategory,
    RuleCheck,
    RuleStatus,
    Severity,
    ValidationIssue,
)

from .facts import sentences

_PROSE = (ClaimKind.PARAGRAPH, ClaimKind.BULLET, ClaimKind.STEP)
_WORD = re.compile(r"\{\{ref:[^}]*\}\}|[A-Za-z0-9][A-Za-z0-9'’./-]*")
_MODAL_TERMS = {"may", "must", "shall", "should", "can", "could", "might", "will", "would"}
_BE = r"(?:is|are|was|were|be|been|being)"
_IRREGULAR = ("done made given taken written sent kept held known shown seen found brought built bought caught chosen "
              "drawn driven eaten fallen forgotten frozen gotten hidden laid led left lost meant paid put read run said "
              "set shut sold spent split spread stolen stood struck sworn taught told thought thrown understood won "
              "withdrawn worn").split()
_PASSIVE = re.compile(rf"\b{_BE}\s+(?:\w+ly\s+)?(?:\w+ed|{'|'.join(_IRREGULAR)})\b", re.I)


def words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def syllables(word: str) -> int:
    """English syllable estimate: vowel groups, minus a silent final 'e'; at least one."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 1
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def claim_sentences(claim: Claim) -> list[str]:
    return sentences(claim.text) or ([claim.text] if claim.text.strip() else [])


def readability(texts: Iterable[str]) -> Optional[tuple[float, float]]:
    """(Flesch Reading Ease, Flesch-Kincaid grade) over the sentences of ``texts``; None without words."""
    sents = [s for t in texts for s in (sentences(t) or [t]) if words(s)]
    ws = [w for s in sents for w in words(s) if not w.startswith("{{")]
    if not sents or not ws:
        return None
    syl = sum(syllables(w) for w in ws)
    wps, spw = len(ws) / len(sents), syl / len(ws)
    return round(206.835 - 1.015 * wps - 84.6 * spw, 1), round(0.39 * wps + 11.8 * spw - 15.59, 1)


def is_passive(sentence: str) -> bool:
    return bool(_PASSIVE.search(sentence))


def style_rules(rules: Iterable[GwpRule]) -> list[GwpRule]:
    return [r for r in rules if r.category == RuleCategory.STYLE and r.check == RuleCheck.DETERMINISTIC
            and r.status == RuleStatus.APPROVED and (r.params or {}).get("kind")]


@dataclass
class CheckedClaim:
    claim: Claim
    section_id: str
    slot_id: str
    rules: list[GwpRule]
    source_text: str  # the cited passages, for the PRES-004 exception


@dataclass
class StyleResult:
    issues: list[ValidationIssue] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


def _issue(key: str, message: str, section: Optional[str], slot: Optional[str] = None,
           claim_ids: Optional[list[str]] = None, unit_ids: Optional[list[str]] = None) -> ValidationIssue:
    digest = hashlib.sha1(f"style|{key}".encode()).hexdigest()[:10]
    return ValidationIssue(issue_id=f"ISS-{digest}", severity=Severity.LOW, category=IssueCategory.SEMANTIC,
                           source=IssueSource.DETERMINISTIC, target_section_id=section, slot_id=slot,
                           claim_ids=claim_ids or [], unit_ids=unit_ids or [], message=message)


def _term_found(term: str, text: str, case_sensitive: bool) -> bool:
    flags = 0 if case_sensitive else re.I
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, flags) is not None


def claim_violations(item: CheckedClaim) -> list[tuple[GwpRule, str]]:
    out = []
    for rule in item.rules:
        params = rule.params or {}
        kind = params.get("kind")
        if kind == "max_sentence_words":
            long = [len(words(s)) for s in claim_sentences(item.claim) if len(words(s)) > params["max_words"]]
            if long:
                out.append((rule, f"{len(long)} sentence(s) over {params['max_words']} words ({', '.join(map(str, long))})"))
        elif kind == "forbidden_terms":
            cs = bool(params.get("case_sensitive"))
            hits = [t for t in params.get("terms", []) if _term_found(t, item.claim.text, cs)
                    and not (t.lower() in _MODAL_TERMS and _term_found(t, item.source_text, cs))]
            if hits:
                prefer = f"; prefer {', '.join(params['prefer'])}" if params.get("prefer") else ""
                out.append((rule, f"uses {', '.join(repr(h) for h in hits)}{prefer}"))
    return out


def check_style(items: list[CheckedClaim]) -> StyleResult:
    """Per-claim and per-section style findings, plus the soft scores for the whole job."""
    result = StyleResult()
    if not items:
        return result
    failing = 0
    for item in items:
        found = claim_violations(item)
        if not found:
            continue
        failing += 1
        applied = set(item.claim.rule_ids_applied)
        text = "; ".join(f"{r.rule_id} {why}" + (" (the draft says it applied this rule)" if r.rule_id in applied else "")
                         for r, why in found)
        result.issues.append(_issue(f"claim|{item.claim.claim_id}|{text}", f"[GWP] {item.claim.claim_id}: {text}.",
                                    item.section_id, item.slot_id, [item.claim.claim_id], list(item.claim.source_unit_ids)))
    result.scores["gwp_style_compliance"] = round(1 - failing / len(items), 3)

    by_section: dict[str, list[CheckedClaim]] = {}
    for item in items:
        by_section.setdefault(item.section_id, []).append(item)
    all_sents = [s for i in items for s in claim_sentences(i.claim)]
    if all_sents:
        result.scores["passive_ratio"] = round(sum(map(is_passive, all_sents)) / len(all_sents), 3)
    overall = readability(i.claim.text for i in items)
    if overall:
        result.scores["flesch_reading_ease"], result.scores["fk_grade"] = overall
    for section_id, group in by_section.items():
        rules = {r.rule_id: r for i in group for r in i.rules}
        sents = [s for i in group for s in claim_sentences(i.claim)]
        score = readability(i.claim.text for i in group)
        for rule in rules.values():
            params = rule.params or {}
            if params.get("kind") == "passive_ratio" and sents:
                ratio = sum(map(is_passive, sents)) / len(sents)
                if ratio > params["max_ratio"]:
                    result.issues.append(_issue(f"passive|{section_id}|{rule.rule_id}",
                                                f"[GWP] {rule.rule_id}: {ratio:.0%} of the sentences in {section_id} are passive "
                                                f"(at most {params['max_ratio']:.0%}).", section_id))
            elif params.get("kind") == "readability" and score:
                flesch, grade = score
                problems = []
                if params.get("flesch_reading_ease_min") is not None and flesch < params["flesch_reading_ease_min"]:
                    problems.append(f"Flesch Reading Ease {flesch} (at least {params['flesch_reading_ease_min']})")
                if params.get("fk_grade_max") is not None and grade > params["fk_grade_max"]:
                    problems.append(f"grade level {grade} (at most {params['fk_grade_max']})")
                if problems:
                    result.issues.append(_issue(f"readability|{section_id}|{rule.rule_id}",
                                                f"[GWP] {rule.rule_id}: {section_id} reads at {' and '.join(problems)}.", section_id))
    return result


def prose(claim: Claim) -> bool:
    return claim.kind in _PROSE and not claim.is_gap_marker

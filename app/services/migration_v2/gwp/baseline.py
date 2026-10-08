"""Built-in preservation rules (migration_plan.md section 4.3).

A writing guide says how to write, not what a migration must keep, so these
invariants come from the system, not the GWP document. They are merged into
every rule set as approved ``baseline`` rules, and they win over any style
rule: a style rewrite must never change what the source says.
"""

from __future__ import annotations

from app.schemas.v2 import GwpRule, RuleCategory, RuleCheck, RuleOrigin, RuleStatus, Severity


def _rule(rule_id: str, text: str, severity: Severity, check: RuleCheck, params: dict | None = None) -> GwpRule:
    return GwpRule(
        rule_id=rule_id,
        category=RuleCategory.PRESERVATION,
        text=text,
        severity=severity,
        check=check,
        params=params or {},
        status=RuleStatus.APPROVED,
        origin=RuleOrigin.BASELINE,
    )


BASELINE_RULES: tuple[GwpRule, ...] = (
    _rule(
        "PRES-001", "Preserve numerical values and percentages exactly.",
        Severity.CRITICAL, RuleCheck.DETERMINISTIC,
        {"kind": "protected_values", "fact_kinds": ["number", "percentage"]},
    ),
    _rule(
        "PRES-002", "Preserve dates, durations, frequencies and deadlines exactly.",
        Severity.CRITICAL, RuleCheck.DETERMINISTIC,
        {"kind": "protected_values", "fact_kinds": ["date", "duration", "frequency", "deadline"]},
    ),
    _rule("PRES-003", "Do not infer roles or responsibilities that the source does not state.", Severity.HIGH, RuleCheck.SEMANTIC),
    _rule(
        "PRES-004",
        "Preserve obligation strength (must, shall, should, may, must not). A style rule never changes it; "
        "flag weak wording such as 'may' for the reviewer instead of strengthening it.",
        Severity.CRITICAL, RuleCheck.DETERMINISTIC, {"kind": "modality"},
    ),
    _rule("PRES-005", "Preserve conditions, exceptions and approval dependencies.", Severity.HIGH, RuleCheck.SEMANTIC),
    _rule(
        "PRES-006", "Preserve document, form and system references (e.g. BI-VQD-10095-S) exactly.",
        Severity.HIGH, RuleCheck.DETERMINISTIC, {"kind": "protected_values", "fact_kinds": ["reference"]},
    ),
)

BASELINE_IDS = frozenset(rule.rule_id for rule in BASELINE_RULES)


def baseline_rules() -> list[GwpRule]:
    return [rule.model_copy(deep=True) for rule in BASELINE_RULES]

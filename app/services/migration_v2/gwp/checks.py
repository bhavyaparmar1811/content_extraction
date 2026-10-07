"""Vocabulary of deterministic GWP checks.

A rule with ``check: deterministic`` names one of these kinds in
``params["kind"]``, with the listed parameters. The validators (Phase 10) run
them without an LLM. Anything a rule cannot express this way is ``semantic``.
"""

from __future__ import annotations

from typing import Any

from app.schemas.v2 import CalloutKind, FactKind, GwpRule, RuleCheck

# kind -> (required params, optional params)
CHECK_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # Words or phrases that must not appear; "prefer" lists replacements to suggest.
    "forbidden_terms": (frozenset({"terms"}), frozenset({"prefer", "case_sensitive"})),
    "max_sentence_words": (frozenset({"max_words"}), frozenset()),
    # Share of sentences in passive voice, 0..1.
    "passive_ratio": (frozenset({"max_ratio"}), frozenset()),
    "readability": (frozenset(), frozenset({"flesch_reading_ease_min", "fk_grade_max"})),
    # Callout kind -> fill colour, compared with the rendered callout shading.
    "callout_palette": (frozenset({"colors"}), frozenset()),
    # Same obligation strength (must/should/may...) in source and draft.
    "modality": (frozenset(), frozenset()),
    # Protected values of these FactKinds are carried over unchanged.
    "protected_values": (frozenset({"fact_kinds"}), frozenset()),
}

# Allowed on every kind: how the reviewer read the guide (e.g. "two lines ~ 30 words").
COMMON_OPTIONAL = frozenset({"note"})


def check_params_problems(rule: GwpRule) -> list[str]:
    """Why a deterministic rule's params cannot run; empty when they can."""
    if rule.check != RuleCheck.DETERMINISTIC:
        return []
    params: dict[str, Any] = rule.params or {}
    kind = params.get("kind")
    if kind not in CHECK_KINDS:
        return [f"unknown check kind {kind!r}; expected one of {sorted(CHECK_KINDS)}"]
    required, optional = CHECK_KINDS[kind]
    given = set(params) - {"kind"} - COMMON_OPTIONAL
    problems = [f"missing param {name!r}" for name in sorted(required - given)]
    problems += [f"unexpected param {name!r}" for name in sorted(given - required - optional)]
    if problems:
        return problems

    if kind == "forbidden_terms":
        terms = params["terms"]
        if not isinstance(terms, list) or not terms or not all(isinstance(t, str) and t.strip() for t in terms):
            problems.append("'terms' must be a non-empty list of strings")
    elif kind == "max_sentence_words":
        if not isinstance(params["max_words"], int) or params["max_words"] < 1:
            problems.append("'max_words' must be a positive integer")
    elif kind == "passive_ratio":
        if not isinstance(params["max_ratio"], (int, float)) or not 0 <= params["max_ratio"] <= 1:
            problems.append("'max_ratio' must be between 0 and 1")
    elif kind == "readability":
        if not given:
            problems.append("give 'flesch_reading_ease_min' and/or 'fk_grade_max'")
    elif kind == "callout_palette":
        colors = params["colors"]
        kinds = {k.value for k in CalloutKind}
        if not isinstance(colors, dict) or not colors:
            problems.append("'colors' must map callout kinds to hex colours")
        else:
            problems += [f"unknown callout kind {k!r}" for k in colors if k not in kinds]
    elif kind == "protected_values":
        fact_kinds = {k.value for k in FactKind}
        values = params["fact_kinds"]
        if not isinstance(values, list) or not values:
            problems.append("'fact_kinds' must be a non-empty list")
        else:
            problems += [f"unknown fact kind {k!r}" for k in values if k not in fact_kinds]
    return problems


def describe_check_kinds() -> str:
    """One line per kind, for prompts."""
    lines = []
    for kind, (required, optional) in CHECK_KINDS.items():
        req = ", ".join(sorted(required)) or "none"
        opt = ", ".join(sorted(optional)) or "none"
        lines.append(f"- {kind}: required params [{req}], optional [{opt}]")
    lines.append(f"Every kind also accepts: {', '.join(sorted(COMMON_OPTIONAL))}.")
    return "\n".join(lines)

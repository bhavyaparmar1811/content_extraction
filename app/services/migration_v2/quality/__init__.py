"""Quality for migration v2: protected facts and deterministic preservation checks (Phase 6).

- ``normalize``: canonical forms (numbers, units, dates, frequencies, qualifiers).
- ``facts``: the protected-fact registry of a source document (``ProtectedFacts``).
- ``preservation``: comparators that check drafted claims against it, returning ``ValidationIssue``s.
"""

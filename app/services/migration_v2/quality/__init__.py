"""Quality for migration v2.

Phase 6, protected facts and deterministic preservation checks:
- ``normalize``: canonical forms (numbers, units, dates, frequencies, qualifiers).
- ``facts``: the protected-fact registry of a source document (``ProtectedFacts``).
- ``preservation``: comparators that check drafted claims against it, returning ``ValidationIssue``s.

Phase 10, the quality loop:
- ``validator``: every deterministic check of the drafts (structure, traceability, preservation, order,
  formatting, callouts) and the GWP style checks (``style``).
- ``critic``: the advisory LLM review of reworded claims.
- ``repair``: re-drafts only the passages an issue points at.
- ``risk`` and ``gates``: high-risk content, the hard gates and the job's ``QualityReport``.
"""

"""Drafting for migration v2 (Phase 9).

- ``refs``: ``{{ref:...}}`` tokens for internal cross-references, and the reverse for comparisons.
- ``memory``: bounded drafting memory (terms, role names, reference targets).
- ``drafter``: slot plan → ``SectionDraft``s (verbatim copy in placement mode, GWP rewrite otherwise).
- ``checks``: deterministic checks of the drafts (coverage, citations, references, gaps, preservation).
"""

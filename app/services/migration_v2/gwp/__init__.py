"""GWP (Good Writing Practice) ingestion and rule catalog for migration v2 (Phase 4).

- ``ingest``: parse the guide (.docx/.pdf) into citable source units.
- ``extractor``: LLM pass → candidate rules, validated and numbered, plus a review report.
- ``baseline``: built-in preservation rules merged into every rule set.
- ``checks``: the deterministic check kinds a rule's ``params`` can name.
- ``service``: files, review, approval and ``select_rules`` for later phases.
"""

"""Template model extraction for migration v2 (Phase 3).

- ``callout_palette``: the template's colour-coded callout boxes.
- ``slot_detector``: sections, slots and anchors → ``TemplateModel``.
- ``overrides``: the human config (``data/template_config/{uid}.json``).
- ``normalizer``: a copy with every slot in a tagged content control.
- ``readiness``: the report that gates migration.
- ``service``: detect → overrides → normalize → readiness, and the saved files.
"""

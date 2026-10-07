"""Planning for migration v2.

- ``signals``: deterministic name and content signals for matching source sections to template sections.
- ``section_planner``: the Level 1 planner (rules propose, one compact LLM call confirms or corrects).
- ``section_validator``: deterministic checks of a section plan; gate issues block approval.
"""

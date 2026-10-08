"""Planning for migration v2.

- ``signals``: deterministic name and content signals for matching source sections to template sections.
- ``section_planner``: the Level 1 planner (rules propose, one compact LLM call confirms or corrects).
- ``section_validator``: deterministic checks of a section plan; gate issues block approval.
- ``slot_signals``: deterministic passage → slot signals (cue words from slot instructions, tables, icon rows).
- ``slot_planner``: the Level 2 planner inside each section mapping (rules place, the LLM confirms per block).
- ``budget``: token estimates and packing of sections into coherent LLM blocks.
- ``slot_validator``: deterministic checks of a slot plan; gate issues block approval.
"""

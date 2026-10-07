"""Human corrections to a detected template: ``data/template_config/{template_uid}.json``.

Example::

    {
      "callout_palette": {"D2F2F7": "introduction"},
      "regions": {"p:32": "fixed", "t:7": "instruction"},
      "sections": {"DISTRIBUTION_OF_CONTROLLED_PRINTS": {"required": false},
                   "ROLES": {"one_of": [["roles", "raci"]]}},
      "slots": {"APPLICABILITY.processes": {"required": false, "content_type": "scope"}}
    }

``regions`` decides the ambiguous regions the detector reports, by region ID.
``slots`` is keyed by ``<SECTION_KEY>.<slot_key>``.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field

from app.schemas.v2 import CalloutKind, ContentType, FormattingProfile, InstructionBehavior, TemplateModel
from app.schemas.v2.common import V2Model


class RegionDecision(str, Enum):
    FIXED = "fixed"  # template text kept as it is (blue parts resolved at render time)
    INSTRUCTION = "instruction"  # guidance for the author: followed, then removed
    SLOT = "slot"  # a destination for migrated content
    CONDITIONAL = "conditional"  # kept, removed or completed per migration, from the SOP's content


class SectionOverride(V2Model):
    required: Optional[bool] = None
    one_of: Optional[list[list[str]]] = Field(
        default=None, description="Slot keys of which at least one must be filled; each becomes optional on its own"
    )
    aliases: Optional[list[str]] = Field(
        default=None, description="Source headings that also mean this section, e.g. ['Scope'] for APPLICABILITY"
    )


class SlotOverride(V2Model):
    content_type: Optional[ContentType] = None
    required: Optional[bool] = None
    instruction_behavior: Optional[InstructionBehavior] = None
    formatting_profile: Optional[FormattingProfile] = None


class TemplateConfig(V2Model):
    callout_palette: dict[str, CalloutKind] = Field(default_factory=dict, description="fill hex → callout kind")
    regions: dict[str, RegionDecision] = Field(default_factory=dict, description="region ID → decision")
    sections: dict[str, SectionOverride] = Field(default_factory=dict, description="section key → override")
    slots: dict[str, SlotOverride] = Field(default_factory=dict, description="'SECTION.slot' → override")


def config_path(config_dir: Path, template_uid: str) -> Path:
    return Path(config_dir) / f"{template_uid}.json"


def load_config(path: Path) -> TemplateConfig:
    """The config at *path*, or an empty one when there is no file."""
    path = Path(path)
    if not path.exists():
        return TemplateConfig()
    return TemplateConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_config(path: Path, config: TemplateConfig) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.model_dump(mode="json", exclude_defaults=True), indent=2), encoding="utf-8")


def apply_slot_overrides(model: TemplateModel, config: TemplateConfig) -> list[str]:
    """Apply ``config.slots`` to *model* in place; returns refs that match no slot.

    A section's ``required`` override is applied during detection, because it
    feeds its slots' defaults; ``one_of`` groups are applied here.
    """
    unknown = []
    for ref, override in config.slots.items():
        slot = model.slot_by_ref(ref)
        if slot is None:
            unknown.append(ref)
            continue
        for name, value in override.model_dump(exclude_none=True).items():
            setattr(slot, name, value)
    sections = {s.key: s for s in model.sections}
    for key, override in config.sections.items():
        section = sections.get(key)
        if section is None:
            unknown.append(f"section {key}")
            continue
        if override.aliases:
            section.aliases = list(dict.fromkeys(section.aliases + override.aliases))
        for group in override.one_of or []:
            slots = {s.key: s for s in section.slots}
            missing = [k for k in group if k not in slots]
            if missing:
                unknown.extend(f"{key}.{k}" for k in missing)
                continue
            section.one_of.append(list(group))
            for k in group:
                slots[k].required = False
    return unknown

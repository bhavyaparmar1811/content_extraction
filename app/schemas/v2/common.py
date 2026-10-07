"""Shared base model and enums for the v2 migration contracts."""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


SCHEMA_VERSION = "v2.0"


class V2Model(BaseModel):
    """Base for all v2 contracts: unknown fields are rejected so drift is caught early."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def to_clean_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)


class ContentType(str, Enum):
    """Normalized semantic category of a target slot (migration_plan.md §6.3)."""

    PURPOSE = "purpose"
    SCOPE = "scope"
    DEFINITION = "definition"
    RESPONSIBILITY = "responsibility"
    PREREQUISITE = "prerequisite"
    PROCESS_INPUT = "process_input"
    ORDERED_PROCEDURE = "ordered_procedure"
    DECISION = "decision"
    TIMING = "timing"
    FREQUENCY = "frequency"
    DURATION = "duration"
    RESTRICTION = "restriction"
    WARNING = "warning"
    EXPECTED_OUTPUT = "expected_output"
    RECORD = "record"
    REFERENCE = "reference"
    SUPPORTING_INFORMATION = "supporting_information"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class CalloutKind(str, Enum):
    """Semantic kind of a template callout box; the colour comes from the template palette."""

    INTRODUCTION = "introduction"
    EXPLANATION = "explanation"
    ATTENTION = "attention"
    KEY_TAKEAWAY = "key_takeaway"


def _normalize_hex(value: str) -> str:
    v = value.strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", v):
        raise ValueError(f"not a 6-digit hex colour: {value!r}")
    return v


HexColor = Annotated[str, AfterValidator(_normalize_hex)]
"""RRGGBB, upper-cased, without '#'. Matches the w:shd/@w:fill form."""

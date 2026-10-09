"""Audit of LLM calls (Phase 12): one ``llm_call`` event per call, written to ``migration_events``.

Every v2 stage gets its chains from ``chain_factory.create_structured_planner``. ``AuditedChainFactory`` wraps that
factory for one stage run, so each call is recorded without the stage code knowing:
- ``task`` and ``prompt_version``, recognised from the call's system prompt (the versioned prompt modules);
- ``model``: the factory's label;
- ``input_tokens`` / ``output_tokens`` from the provider's usage, and ``latency_ms``;
- ``retry``: true for a second attempt (a repair or retry message added, or a failed attempt before it), and
  ``status`` ``ok`` / ``error`` (a failed attempt is recorded too, with its error);
- ``unit_ids``: the source passages (``SRC-…-U…``) and claims (``C-…``) the prompt supplied, capped.

The orchestrator (and the inspection runner) writes the recorded calls as events after each stage.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from app.services.llm.prompts.v2 import critic, drafter, reconcile, section_planner, slot_planner

_MAX_IDS = 300
_UNIT = re.compile(r"\bSRC-[\w.]+?-U\d{3,}\b")
_CLAIM = re.compile(r"\bC-[A-Z]+-[\w.]+-\d{3,}\b")
_RETRY_MARKS = ("SECOND ATTEMPT", "REPAIR:", "previous answer", "Your answer had problems")

_PROMPTS: list[tuple[str, str, str]] = [  # (system prompt, task, version)
    (section_planner.SECTION_PLANNER_SYSTEM_PROMPT, "section_planner", section_planner.PROMPT_VERSION),
    (slot_planner.SLOT_PLANNER_SYSTEM_PROMPT, "slot_planner", slot_planner.PROMPT_VERSION),
    (drafter.REWRITE_SYSTEM_PROMPT, "drafter_rewrite", drafter.PROMPT_VERSION),
    (drafter.SPLIT_SYSTEM_PROMPT, "drafter_split", drafter.PROMPT_VERSION),
    (critic.SYSTEM_PROMPT, "critic", critic.PROMPT_VERSION),
    (reconcile.SYSTEM_PROMPT, "reconcile", reconcile.PROMPT_VERSION),
]


def _content(message: Any) -> str:
    content = getattr(message, "content", message)
    return content if isinstance(content, str) else str(content)


def describe(messages: list) -> dict[str, Any]:
    """Task, prompt version, retry flag and supplied IDs of one call's messages."""
    system = _content(messages[0]) if messages else ""
    task, version = next(((t, v) for prompt, t, v in _PROMPTS if system == prompt), ("unknown", None))
    if task == "drafter_rewrite" and any("REPAIR:" in _content(m) for m in messages[1:]):
        task = "repair"
    rest = " ".join(_content(m) for m in messages[1:])
    retry = len(messages) > 2 or any(mark in rest for mark in _RETRY_MARKS)
    return {
        "task": task, "prompt_version": version, "retry": retry,
        "unit_ids": list(dict.fromkeys(_UNIT.findall(rest)))[:_MAX_IDS],
        "claim_ids": list(dict.fromkeys(_CLAIM.findall(rest)))[:_MAX_IDS],
    }


def _usage(result: Any) -> dict[str, int]:
    raw = result.get("raw") if isinstance(result, dict) else None
    meta = getattr(raw, "usage_metadata", None) or {}
    return {k: int(meta.get(k, 0)) for k in ("input_tokens", "output_tokens") if k in meta}


class AuditedChain:
    def __init__(self, chain: Any, model: Optional[str], schema: str, sink: list[dict]):
        self._chain, self._model, self._schema, self._sink = chain, model, schema, sink

    async def ainvoke(self, messages, *args, **kwargs):
        record = {**describe(list(messages)), "model": self._model, "schema": self._schema}
        start = time.perf_counter()
        try:
            result = await self._chain.ainvoke(messages, *args, **kwargs)
        except Exception as exc:
            self._sink.append({**record, "status": "error", "error": str(exc)[:300],
                               "latency_ms": round((time.perf_counter() - start) * 1000)})
            raise
        parsed_ok = not (isinstance(result, dict) and "parsed" in result and result.get("parsed") is None)
        self._sink.append({**record, **_usage(result), "status": "ok" if parsed_ok else "unparseable",
                           "latency_ms": round((time.perf_counter() - start) * 1000)})
        return result

    def __getattr__(self, name):
        return getattr(self._chain, name)


class AuditedChainFactory:
    """Wraps a chain factory for one stage run; ``calls`` collects one record per LLM call."""

    def __init__(self, factory: Any):
        self._factory = factory
        self.calls: list[dict] = []

    def create_structured_planner(self, schema, *args, **kwargs):
        chain = self._factory.create_structured_planner(schema, *args, **kwargs)
        label = self._factory.planner_label() if hasattr(self._factory, "planner_label") else None
        return AuditedChain(chain, label, getattr(schema, "__name__", str(schema)), self.calls)

    def __getattr__(self, name):
        return getattr(self._factory, name)


def audited(factory: Any) -> Optional[AuditedChainFactory]:
    return AuditedChainFactory(factory) if factory is not None else None


def write_calls(store: Any, job_id: str, actor: str, factory: Any, stage: str) -> int:
    """Write the calls an ``AuditedChainFactory`` recorded as ``llm_call`` events; returns how many."""
    calls = getattr(factory, "calls", None) or []
    for i, call in enumerate(calls):
        store.add_event(job_id, "llm_call", actor, {"stage": stage, "call": i + 1, **call})
    count = len(calls)
    if calls:
        calls.clear()
    return count

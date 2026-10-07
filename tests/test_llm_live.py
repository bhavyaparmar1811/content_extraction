"""Live checks against the configured LLM provider (Azure OpenAI or another).

Skipped unless SOP_RUN_LLM_TESTS=1. They send two short synthetic prompts,
no document content, and confirm plain and structured output both work with
the configured model, deployment and API version.

    SOP_RUN_LLM_TESTS=1 pytest -m llm tests/test_llm_live.py
"""

import os

import pytest
from pydantic import BaseModel, Field

from app.config.settings import get_settings
from app.services.llm.chain_factory import ChainFactory

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(os.environ.get("SOP_RUN_LLM_TESTS") != "1", reason="set SOP_RUN_LLM_TESTS=1 to call the LLM"),
]


class _Obligation(BaseModel):
    actor: str = Field(description="Who must act")
    action: str = Field(description="What they must do")
    deadline: str = Field(description="The time limit, as written")


@pytest.fixture(scope="module")
def factory():
    return ChainFactory(get_settings())


def test_plain_call(factory):
    reply = factory.create_planner_model().invoke("Reply with the single word OK.")
    assert "ok" in reply.content.strip().lower()


def test_structured_output(factory):
    planner = factory.create_structured_planner(_Obligation)
    result = planner.invoke(
        "Extract the obligation from this sentence: "
        "'The QA reviewer must approve the change request within five business days.'"
    )
    assert isinstance(result, _Obligation)
    assert "qa" in result.actor.lower()
    assert "five" in result.deadline.lower() or "5" in result.deadline

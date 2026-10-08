"""Tests for model-string routing in ChainFactory."""

import pytest

from app.services.llm.chain_factory import resolve_model


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("gemini/gemini-2.5-flash", ("gemini-2.5-flash", "google_genai")),
        ("google_genai:gemini-2.5-flash", ("gemini-2.5-flash", "google_genai")),
        ("openai/gpt-4o", ("gpt-4o", "openai")),
        ("anthropic/claude-sonnet-5-5", ("claude-sonnet-5-5", "anthropic")),
        ("gpt-4o", ("gpt-4o", None)),
        ("unknown/some-model", ("unknown/some-model", None)),
    ],
)
def test_resolve_model(configured, expected):
    assert resolve_model(configured) == expected


# ── Azure settings ─────────────────────────────────────────────────────

_AZURE_VARS = [
    "AZURE_OPENAI_API_KEY", "SOP_AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT", "SOP_AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION", "SOP_AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "SOP_AZURE_OPENAI_PLANNER_DEPLOYMENT", "SOP_AZURE_OPENAI_SUMMARIZER_DEPLOYMENT",
    "SOP_USE_AZURE_OPENAI",
]


@pytest.fixture
def clean_env(monkeypatch):
    """Isolate from the developer's .env, which settings.py loads into os.environ."""
    for name in _AZURE_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings():
    from app.config.settings import Settings

    return Settings(_env_file=None)


def _standard_azure_env(env):
    env.setenv("AZURE_OPENAI_ENDPOINT", "https://example-resource.openai.azure.com/")
    env.setenv("AZURE_OPENAI_API_KEY", "test-key")
    env.setenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    env.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-poc")


def test_standard_azure_names_are_read(clean_env):
    _standard_azure_env(clean_env)
    s = _settings()
    assert s.use_azure_openai is True
    assert s.azure_openai_endpoint == "https://example-resource.openai.azure.com/"
    assert s.azure_openai_api_version == "2025-01-01-preview"
    assert s.azure_openai_planner_deployment == "gpt-4o-poc"
    assert s.azure_openai_summarizer_deployment == "gpt-4o-poc"


def test_sop_names_win_over_standard_names(clean_env):
    _standard_azure_env(clean_env)
    clean_env.setenv("SOP_AZURE_OPENAI_SUMMARIZER_DEPLOYMENT", "gpt-4o-mini-poc")
    clean_env.setenv("SOP_AZURE_OPENAI_ENDPOINT", "https://other.openai.azure.com/")
    s = _settings()
    assert s.azure_openai_planner_deployment == "gpt-4o-poc"
    assert s.azure_openai_summarizer_deployment == "gpt-4o-mini-poc"
    assert s.azure_openai_endpoint == "https://other.openai.azure.com/"


def test_azure_switch(clean_env):
    assert _settings().use_azure_openai is False  # nothing configured
    clean_env.setenv("AZURE_OPENAI_API_KEY", "test-key")
    assert _settings().use_azure_openai is False  # key without endpoint
    clean_env.setenv("AZURE_OPENAI_ENDPOINT", "https://example-resource.openai.azure.com/")
    assert _settings().use_azure_openai is True
    clean_env.setenv("SOP_USE_AZURE_OPENAI", "false")
    assert _settings().use_azure_openai is False  # explicit setting wins


def test_factory_builds_azure_model_for_the_deployment(clean_env):
    from langchain_openai import AzureChatOpenAI

    from app.services.llm.chain_factory import ChainFactory

    _standard_azure_env(clean_env)
    model = ChainFactory(_settings()).create_planner_model()  # no request is sent
    assert isinstance(model, AzureChatOpenAI)
    assert model.deployment_name == "gpt-4o-poc"
    assert model.azure_endpoint == "https://example-resource.openai.azure.com/"

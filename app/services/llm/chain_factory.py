"""LangChain model factory for SOP Content Migration.

Supports four provider paths — all controlled entirely through .env / Settings:

  Provider           | use_azure_openai | llm_planner_model (prefix)
  -------------------|------------------|---------------------------
  Google Gemini      | false            | "gemini/..."
  OpenAI             | false            | "openai/..."
  Anthropic          | false            | "anthropic/..."
  Azure OpenAI       | true             | (ignored — uses deployment names)

Azure routing:
  On automatically when an endpoint and AZURE_OPENAI_API_KEY are set; force it
  with SOP_USE_AZURE_OPENAI=true/false. Supply:
    AZURE_OPENAI_API_KEY        (read from env directly — no SOP_ prefix)
    SOP_AZURE_OPENAI_ENDPOINT   or AZURE_OPENAI_ENDPOINT, e.g. "https://<your-resource>.openai.azure.com/"
    SOP_AZURE_OPENAI_API_VERSION or AZURE_OPENAI_API_VERSION
    SOP_AZURE_OPENAI_PLANNER_DEPLOYMENT    or AZURE_OPENAI_DEPLOYMENT_NAME (shared)
    SOP_AZURE_OPENAI_SUMMARIZER_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT_NAME (shared)
  The SOP_ name wins when both are set.
"""

from __future__ import annotations

import os
from typing import Any, Type
from pydantic import BaseModel
from langchain.chat_models import init_chat_model
from app.config.settings import Settings


# Maps the "provider/model" prefix used in .env to LangChain's provider id.
# Without this, init_chat_model infers "gemini/..." as google_vertexai and
# passes the whole string (prefix included) as the model name.
_PROVIDER_ALIASES = {
    "gemini": "google_genai",
    "google": "google_genai",
    "google_genai": "google_genai",
    "openai": "openai",
    "anthropic": "anthropic",
}


def resolve_model(model: str) -> tuple[str, str | None]:
    """Split a configured model string into (model_name, langchain_provider).

    Accepts "provider/model" (documented .env format) and "provider:model"
    (LangChain format). Unknown prefixes are left for LangChain to infer.
    """
    for sep in ("/", ":"):
        if sep in model:
            prefix, name = model.split(sep, 1)
            provider = _PROVIDER_ALIASES.get(prefix.strip().lower())
            if provider:
                return name.strip(), provider
    return model, None


class ChainFactory:
    """Creates configured LangChain chat models from application settings."""

    def __init__(self, settings: Settings):
        self.settings = settings

    # ── Public model constructors ─────────────────────────────────────────

    def create_planner_model(self) -> Any:
        """ChatModel for the global migration planner (Phase 2)."""
        if self.settings.use_azure_openai:
            return self._create_azure_model(
                deployment=self.settings.azure_openai_planner_deployment,
                max_tokens=self.settings.llm_planner_max_tokens,
            )
        model, provider = resolve_model(self.settings.llm_planner_model)
        return init_chat_model(
            model=model,
            model_provider=provider,
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.llm_planner_max_tokens,
        )

    def create_summarizer_model(self) -> Any:
        """ChatModel for the per-section semantic summarizer (Mode B)."""
        if self.settings.use_azure_openai:
            return self._create_azure_model(
                deployment=self.settings.azure_openai_summarizer_deployment,
                max_tokens=self.settings.llm_summarizer_max_tokens,
            )
        model, provider = resolve_model(self.settings.llm_summarizer_model)
        return init_chat_model(
            model=model,
            model_provider=provider,
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.llm_summarizer_max_tokens,
        )

    def create_structured_planner(self, output_schema: Type[BaseModel], include_raw: bool = False) -> Any:
        """Planner with structured JSON output bound to a Pydantic schema.

        With ``include_raw`` the chain returns ``{"raw", "parsed", "parsing_error"}``,
        so callers can read token usage from ``raw.usage_metadata``.
        """
        return self.create_planner_model().with_structured_output(output_schema, include_raw=include_raw)

    def create_structured_summarizer(self, output_schema: Type[BaseModel]) -> Any:
        """Summarizer with structured JSON output bound to a Pydantic schema."""
        return self.create_summarizer_model().with_structured_output(output_schema)

    def planner_label(self) -> str:
        """A readable name of the planner model, recorded on artifacts ("azure/<deployment>" or "provider/model")."""
        if self.settings.use_azure_openai:
            return f"azure/{self.settings.azure_openai_planner_deployment}"
        return self.settings.llm_planner_model

    # ── Private helpers ───────────────────────────────────────────────────

    def _create_azure_model(self, deployment: str, max_tokens: int) -> Any:
        """Build an AzureChatOpenAI instance from settings and env variables."""
        from langchain_openai import AzureChatOpenAI

        api_key = self.settings.azure_openai_api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "AZURE_OPENAI_API_KEY environment variable is not set. "
                "Add it to your .env file without any SOP_ prefix."
            )

        endpoint = self.settings.azure_openai_endpoint
        if not endpoint:
            raise EnvironmentError(
                "SOP_AZURE_OPENAI_ENDPOINT is not configured. "
                "Set it to your Azure resource URL, e.g. "
                "'https://<your-resource>.openai.azure.com/'"
            )

        return AzureChatOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            openai_api_version=self.settings.azure_openai_api_version,
            api_key=api_key,
            temperature=self.settings.llm_temperature,
            max_tokens=max_tokens,
        )

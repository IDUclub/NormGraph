"""Provider factories — build the configured LLM and embedder from ``Settings``.

A single switch (``NG_LLM_PROVIDER`` / ``NG_EMBEDDINGS_PROVIDER``) selects the concrete
implementation, so the rest of the code depends only on the abstract interfaces.
"""

from __future__ import annotations

from src.common.config import Settings
from src.providers.base import Embedder, LLMProvider
from src.providers.embeddings_ollama import OllamaEmbedder
from src.providers.embeddings_openai import OpenAICompatibleEmbedder
from src.providers.llm_ollama import OllamaLLM
from src.providers.llm_openai import OpenAICompatibleLLM


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "ollama":
        return OllamaLLM(
            base_url=settings.ollama_base,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
        )
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLLM(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
        )
    raise ValueError(f"unknown NG_LLM_PROVIDER: {settings.llm_provider!r}")


def build_embedder(settings: Settings) -> Embedder:
    if settings.embeddings_provider == "ollama":
        return OllamaEmbedder(
            base_url=settings.ollama_base,
            model=settings.embeddings_model,
            dim=settings.vector_size,
            timeout=settings.embeddings_timeout,
        )
    if settings.embeddings_provider == "openai_compatible":
        return OpenAICompatibleEmbedder(
            base_url=settings.embeddings_url,
            model=settings.embeddings_model,
            dim=settings.vector_size,
            api_key=settings.embeddings_api_key,
            query_prompt=settings.embeddings_query_prompt,
            timeout=settings.embeddings_timeout,
        )
    raise ValueError(
        f"unknown NG_EMBEDDINGS_PROVIDER: {settings.embeddings_provider!r}"
    )

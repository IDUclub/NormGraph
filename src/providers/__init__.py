"""Pluggable model providers (LLM + embeddings).

The service is deliberately provider-agnostic. Both the chat model (restriction extraction via
langextract + auxiliary reasoning) and the vectorizer are hidden behind small abstract interfaces
(``LLMProvider`` / ``Embedder``), so a deployment can point NormGraph at any OpenAI-compatible
endpoint (vLLM, LM Studio, llama.cpp, Ollama's ``/v1`` shim, ...) or native Ollama by flipping a
single setting. The default is OpenAI-compatible: an OpenAI-style chat endpoint for the LLM and
Giga-Embeddings-instruct (2048-d) for vectors, matching the current IDU_DVD vector space.
"""

from src.providers.base import Embedder, LLMProvider  # noqa: F401
from src.providers.factory import build_embedder, build_llm  # noqa: F401

__all__ = ["LLMProvider", "Embedder", "build_llm", "build_embedder"]

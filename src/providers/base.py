"""Abstract provider interfaces for the LLM and the vectorizer.

Both a sync and an async entry point are exposed on purpose:

* the **async** methods serve the API request path (search enrichment, aux reasoning) without
  blocking the event loop;
* the **sync** methods serve the extraction pipeline, which runs blocking work (langextract) in a
  worker thread — a synchronous call there is simpler and avoids nested event loops.

Concrete providers implement both against the same endpoint/config, so behaviour is identical
whichever path a caller takes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """A chat/completion model behind a uniform interface."""

    #: Model identifier (goes into provenance / embedding_meta-style records).
    model: str

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return the model's text completion for ``prompt`` (async path)."""

    @abstractmethod
    def complete_sync(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return the model's text completion for ``prompt`` (blocking path)."""

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        """Release any pooled connections. Overridden by providers holding clients."""


class Embedder(ABC):
    """A text vectorizer behind a uniform interface.

    Some models (e.g. Giga-Embeddings-instruct) are *asymmetric*: documents are embedded plainly
    while queries are prefixed with an instruction. ``embed_documents`` and ``embed_query`` keep
    that distinction explicit so callers never have to know which model is behind the interface.
    """

    #: Model identifier.
    model: str
    #: Vector dimension produced by this embedder.
    dim: int

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents/passages (async path)."""

    @abstractmethod
    def embed_documents_sync(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents/passages (blocking path)."""

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """Embed a single search query (async path, applies the query instruction)."""

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        """Release any pooled connections. Overridden by providers holding clients."""

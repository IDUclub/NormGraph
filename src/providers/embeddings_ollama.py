"""Native Ollama embeddings provider (``/api/embed``, e.g. bge-m3, 1024-d).

A fallback vectorizer for deployments without the giga-vectorizer service. Ollama has no
asymmetric query prompt, so ``embed_query`` embeds the text as-is.
"""

from __future__ import annotations

import httpx

from src.providers.base import Embedder


class OllamaEmbedder(Embedder):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        dim: int,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self._timeout = timeout
        self._async: httpx.AsyncClient | None = None
        self._sync: httpx.Client | None = None

    def _body(self, texts: list[str]) -> dict:
        return {"model": self.model, "input": texts}

    @staticmethod
    def _extract(data: dict) -> list[list[float]]:
        vectors = data.get("embeddings")
        if not vectors:
            raise RuntimeError("Ollama returned no embeddings")
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._async is None:
            self._async = httpx.AsyncClient(timeout=self._timeout)
        resp = await self._async.post(
            f"{self.base_url}/api/embed", json=self._body(texts)
        )
        resp.raise_for_status()
        return self._extract(resp.json())

    def embed_documents_sync(self, texts: list[str]) -> list[list[float]]:
        if self._sync is None:
            self._sync = httpx.Client(timeout=self._timeout)
        resp = self._sync.post(f"{self.base_url}/api/embed", json=self._body(texts))
        resp.raise_for_status()
        return self._extract(resp.json())

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None
        if self._sync is not None:
            self._sync.close()
            self._sync = None

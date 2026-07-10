"""OpenAI-compatible embeddings provider (default: Giga-Embeddings-instruct, 2048-d).

Targets the ``POST /v1/embeddings`` surface exposed by the giga-vectorizer service, which extends
the OpenAI schema with an optional ``prompt`` field the service prepends to every input. The model
is asymmetric, so documents are embedded with an empty prompt and queries with the configured
instruction — matching how IDU_DVD builds the same vector space.
"""

from __future__ import annotations

import httpx

from src.providers.base import Embedder


class OpenAICompatibleEmbedder(Embedder):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        dim: int,
        api_key: str | None = None,
        query_prompt: str = "",
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self._api_key = api_key
        self._query_prompt = query_prompt
        self._timeout = timeout
        self._async: httpx.AsyncClient | None = None
        self._sync: httpx.Client | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, texts: list[str], prompt: str | None) -> dict:
        body: dict = {"input": texts, "model": self.model}
        if prompt is not None:
            body["prompt"] = prompt
        return body

    @staticmethod
    def _extract(data: dict) -> list[list[float]]:
        items = data.get("data")
        if not items:
            raise RuntimeError("embeddings service returned no data")
        return [item["embedding"] for item in sorted(items, key=lambda d: d["index"])]

    async def _embed(self, texts: list[str], prompt: str | None) -> list[list[float]]:
        if self._async is None:
            self._async = httpx.AsyncClient(timeout=self._timeout)
        resp = await self._async.post(
            f"{self.base_url}/v1/embeddings",
            headers=self._headers(),
            json=self._body(texts, prompt),
        )
        resp.raise_for_status()
        return self._extract(resp.json())

    def _embed_sync(self, texts: list[str], prompt: str | None) -> list[list[float]]:
        if self._sync is None:
            self._sync = httpx.Client(timeout=self._timeout)
        resp = self._sync.post(
            f"{self.base_url}/v1/embeddings",
            headers=self._headers(),
            json=self._body(texts, prompt),
        )
        resp.raise_for_status()
        return self._extract(resp.json())

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, prompt="")

    def embed_documents_sync(self, texts: list[str]) -> list[list[float]]:
        return self._embed_sync(texts, prompt="")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], prompt=self._query_prompt))[0]

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None
        if self._sync is not None:
            self._sync.close()
            self._sync = None

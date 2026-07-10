"""Async HTTP client for IDU_DVD.

Wraps the endpoints NormGraph reads from: the ``/library`` read API (enumerate + fetch a whole
document as ordered fragments), the aggregated ``/documents`` listing, and ``/search`` (used both
for RAG-fallback enrichment and, until the library API surfaces ``references``, to back-fill the
reference edges of a document).
"""

from __future__ import annotations

import httpx
import structlog

from src.dvd_client.models import (
    DocumentDetail,
    DocumentList,
    SearchResponse,
)

log = structlog.get_logger(__name__)


class DVDClient:
    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self._timeout
            )
        return self._client

    async def ping(self) -> bool:
        try:
            resp = await self._http().get("/ping")
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("dvd_unreachable", error=str(exc))
            return False

    async def list_library_documents(self) -> DocumentList:
        """All documents registered in IDU_DVD (identity/corpus metadata)."""
        resp = await self._http().get("/library/documents")
        resp.raise_for_status()
        return DocumentList.model_validate(resp.json())

    async def lookup(self, key: str) -> DocumentList:
        """Resolve documents by an exact lookup key / external id."""
        resp = await self._http().get("/library/lookup", params={"key": key})
        resp.raise_for_status()
        return DocumentList.model_validate(resp.json())

    async def get_document(self, doc_id: str) -> DocumentDetail | None:
        """One document: assembled text + metadata + ordered fragments."""
        resp = await self._http().get(f"/library/documents/{doc_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return DocumentDetail.model_validate(resp.json())

    async def resolve_doc_ids(self, name: str) -> list[str]:
        """Doc ids for a document name (a name may map to several corpus entries)."""
        docs = await self.lookup(name)
        ids = [d.doc_id for d in docs.documents if d.doc_id]
        if ids:
            return ids
        # Fallback: scan the library listing by exact name match.
        listing = await self.list_library_documents()
        return [d.doc_id for d in listing.documents if d.name == name and d.doc_id]

    async def search(
        self,
        query: str,
        *,
        doc_id: str | None = None,
        document_names: list[str] | None = None,
        version: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> SearchResponse:
        """Vector search over text fragments (used for RAG fallback / reference back-fill)."""
        body: dict = {"query": query, "limit": limit}
        if doc_id:
            body["doc_id"] = doc_id
        if document_names:
            body["document_names"] = document_names
        if version:
            body["version"] = version
        if tags:
            body["tags"] = tags
        resp = await self._http().post("/search/texts", json=body)
        resp.raise_for_status()
        return SearchResponse.model_validate(resp.json())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

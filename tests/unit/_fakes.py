"""Shared hermetic fakes for pipeline unit tests."""

from __future__ import annotations

from src.providers.base import Embedder


class FakeEmbedder(Embedder):
    def __init__(self, dim: int = 4) -> None:
        self.model = "fake-embed"
        self.dim = dim

    async def embed_documents(self, texts):
        return [[float(len(t))] * self.dim for t in texts]

    def embed_documents_sync(self, texts):
        return [[float(len(t))] * self.dim for t in texts]

    async def embed_query(self, text):
        return [float(len(text))] * self.dim


class FakeWriter:
    """Records graph writes; returns preconfigured lookup/nearest results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.kind_exact: dict | None = None
        self.entity_exact: dict | None = None
        self.nearest_result: list[dict] = []
        self.shares_entity_result: list[dict] = []

    def _rec(self, op: str, **params) -> None:
        self.calls.append((op, params))

    def named(self, op: str) -> list[dict]:
        return [p for n, p in self.calls if n == op]

    async def get_kind(self, name):
        self._rec("get_kind", name=name)
        return self.kind_exact

    async def get_entity(self, normalized):
        self._rec("get_entity", normalized=normalized)
        return self.entity_exact

    async def nearest(self, index, embedding, k=1):
        self._rec("nearest", index=index, k=k)
        return self.nearest_result

    async def ensure_kind(
        self, name, *, status="approved", aliases=None, embedding=None
    ):
        self._rec(
            "ensure_kind",
            name=name,
            status=status,
            aliases=aliases,
            has_emb=embedding is not None,
        )

    async def upsert_entity(
        self, normalized, *, name, aliases=None, embedding=None, status="active"
    ):
        self._rec(
            "upsert_entity",
            normalized=normalized,
            name=name,
            aliases=aliases,
            has_emb=embedding is not None,
        )

    async def upsert_restriction(
        self,
        props,
        *,
        clause_node_id,
        subject_normalized,
        object_normalized,
        kind_name,
        embedding=None,
    ):
        self._rec(
            "upsert_restriction",
            id=props["id"],
            clause=clause_node_id,
            subject=subject_normalized,
            object=object_normalized,
            kind=kind_name,
            props=props,
        )

    async def link_shares_entity(self, restriction_id):
        self._rec("link_shares_entity", id=restriction_id)
        return self.shares_entity_result

    async def upsert_conflict(self, restriction_id, other_id, *, reason, severity):
        self._rec(
            "upsert_conflict",
            id=restriction_id,
            other_id=other_id,
            reason=reason,
            severity=severity,
        )

    async def get_clauses(self, doc_id):
        self._rec("get_clauses", doc_id=doc_id)
        return getattr(self, "clauses", [])

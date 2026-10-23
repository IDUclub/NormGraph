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
        self.entity_exact: dict | None = None
        self.nearest_result: list[dict] = []
        self.shares_entity_result: list[dict] = []

    def _rec(self, op: str, **params) -> None:
        self.calls.append((op, params))

    def named(self, op: str) -> list[dict]:
        return [p for n, p in self.calls if n == op]

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

    async def duplicate_candidates(self, norm_key, restriction_id, *, doc_id):
        self._rec(
            "duplicate_candidates", key=norm_key, id=restriction_id, doc_id=doc_id
        )
        return getattr(self, "duplicates", [])

    async def set_duplicate_group(self, ids, group):
        self._rec("set_duplicate_group", ids=ids, group=group)

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

    async def clause_contexts(self, doc_id):
        self._rec("clause_contexts", doc_id=doc_id)
        return getattr(self, "contexts", {})

    async def upsert_document(self, props):
        self._rec("upsert_document", **props)

    async def delete_restrictions_of_doc(self, doc_id):
        self._rec("delete_restrictions_of_doc", doc_id=doc_id)

    async def delete_restrictions_of_clauses(self, doc_id, clause_node_ids):
        self._rec(
            "delete_restrictions_of_clauses", doc_id=doc_id, clauses=clause_node_ids
        )

    async def mark_extracted(self, rows):
        self._rec("mark_extracted", rows=rows)

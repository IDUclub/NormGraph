"""Ingestion maps a DVD document to the right graph writes — Neo4j faked."""

from __future__ import annotations

import pytest

from src.dvd_client.models import (
    DocumentDetail,
    DocumentFragment,
    DocumentRef,
    SearchResponse,
)
from src.graph.writer import GraphWriter
from src.ingestion.service import IngestionService


class FakeGraphClient:
    """Records every Cypher call instead of touching a real database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, **params) -> list[dict]:
        self.calls.append((query, params))
        return []

    def queries_containing(self, needle: str) -> list[dict]:
        return [params for query, params in self.calls if needle in query]


class FakeDVD:
    def __init__(self, detail: DocumentDetail) -> None:
        self._detail = detail
        self.search_calls: list[dict] = []

    async def get_document(self, doc_id: str) -> DocumentDetail:
        return self._detail

    async def search(
        self,
        query,
        *,
        doc_id=None,
        document_names=None,
        version=None,
        tags=None,
        limit=10,
    ):
        self.search_calls.append({"query": query, "doc_id": doc_id})
        return SearchResponse(count=0, hits=[])


def _detail() -> DocumentDetail:
    return DocumentDetail(
        doc_id="d1",
        name="СП 42.13330.2016",
        version="2016",
        version_id="v1",
        fragments=[
            DocumentFragment(id="a", order=0, numbering="8", text="root"),
            DocumentFragment(
                id="b",
                order=1,
                numbering="8.3",
                parent_id="a",
                text="clause",
                references=[
                    DocumentRef(
                        raw="СП 52.13330",
                        target_name="СП 52.13330",
                        target_node_id="x",
                        scope="external",
                        resolved=True,
                    ),
                    DocumentRef(
                        raw="СП 99 (not loaded)",
                        target_name="СП 99",
                        target_numbering="4.1",
                        scope="external",
                        resolved=False,
                    ),
                ],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_ingest_document_writes_nodes_and_edges():
    client = FakeGraphClient()
    svc = IngestionService(FakeDVD(_detail()), GraphWriter(client))

    result = await svc.ingest_document("d1")

    assert result.clauses == 2
    assert result.references == 1
    assert result.pending_references == 1

    # One document upsert with the right key.
    doc_calls = client.queries_containing("MERGE (d:Document {doc_id: $doc_id})")
    assert len(doc_calls) == 1
    assert doc_calls[0]["doc_id"] == "d1"

    # Two clause upserts.
    assert len(client.queries_containing("MERGE (c:Clause {node_id: $node_id})")) == 2

    # PART_OF b -> a.
    part_of = client.queries_containing("MERGE (c)-[:PART_OF]->(p)")
    assert part_of == [{"child": "b", "parent": "a"}]

    # Resolved reference points at the target clause node.
    resolved = client.queries_containing("MERGE (t:Clause {node_id: $tid})")
    assert resolved and resolved[0]["tid"] == "x"

    # Unresolved reference lands on a PendingReference stub.
    pending = client.queries_containing("MERGE (p:PendingReference {key: $key})")
    assert pending and pending[0]["key"] == "сп 99#4.1"


@pytest.mark.asyncio
async def test_ingest_missing_document_is_skipped():
    class NoneDVD:
        async def get_document(self, doc_id: str):
            return None

    svc = IngestionService(NoneDVD(), GraphWriter(FakeGraphClient()))
    result = await svc.ingest_document("nope")
    assert result.skipped is True


@pytest.mark.asyncio
async def test_ingest_document_stamps_user_scope():
    client = FakeGraphClient()
    svc = IngestionService(FakeDVD(_detail()), GraphWriter(client))

    await svc.ingest_document("d1", user_id="u1", scenario_id="s1")

    doc_calls = client.queries_containing("MERGE (d:Document {doc_id: $doc_id})")
    assert doc_calls[0]["props"]["user_id"] == "u1"
    assert doc_calls[0]["props"]["scenario_id"] == "s1"


@pytest.mark.asyncio
async def test_ingest_document_omits_scope_when_unset():
    client = FakeGraphClient()
    svc = IngestionService(FakeDVD(_detail()), GraphWriter(client))

    await svc.ingest_document("d1")

    doc_calls = client.queries_containing("MERGE (d:Document {doc_id: $doc_id})")
    assert "user_id" not in doc_calls[0]["props"]
    assert "scenario_id" not in doc_calls[0]["props"]


def _no_refs_detail() -> DocumentDetail:
    return DocumentDetail(
        doc_id="d1",
        name="user doc",
        version="1",
        fragments=[
            DocumentFragment(id="a", order=0, text="clause text without references")
        ],
    )


@pytest.mark.asyncio
async def test_ingest_document_forces_backfill_off_when_scoped():
    dvd = FakeDVD(_no_refs_detail())
    svc = IngestionService(
        dvd, GraphWriter(FakeGraphClient()), backfill_references=True
    )

    await svc.ingest_document("d1", user_id="u1", scenario_id="s1")

    # A scoped (user) document never back-fills references, even with backfill_references=True —
    # mirrors IDU_DVD's own enable_reference_linking=False rule for ad hoc user uploads.
    assert dvd.search_calls == []


@pytest.mark.asyncio
async def test_ingest_document_backfill_runs_for_unscoped_document():
    dvd = FakeDVD(_no_refs_detail())
    svc = IngestionService(
        dvd, GraphWriter(FakeGraphClient()), backfill_references=True
    )

    await svc.ingest_document("d1")

    assert len(dvd.search_calls) == 1

"""Delete / prune graph helpers and the incremental `replace` wiring — Neo4j faked."""

from __future__ import annotations

import pytest

from src.dvd_client.models import DocumentDetail, DocumentFragment
from src.graph.writer import GraphWriter
from src.ingestion.service import IngestionService
from src.pipeline.models import ExtractedRestriction
from src.pipeline.service import ExtractionService


class FakeGraphClient:
    def __init__(self, returns=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._returns = returns or {}

    async def run(self, query: str, **params):
        self.calls.append((query, params))
        for needle, rows in self._returns.items():
            if needle in query:
                return rows
        return []

    def queries_containing(self, needle: str) -> list[dict]:
        return [params for query, params in self.calls if needle in query]


@pytest.mark.asyncio
async def test_delete_document_returns_counts():
    client = FakeGraphClient(
        returns={"DETACH DELETE d": [{"clauses": 3, "restrictions": 5}]}
    )
    counts = await GraphWriter(client).delete_document("d1")
    assert counts == {"clauses": 3, "restrictions": 5}
    assert client.queries_containing("MATCH (d:Document {doc_id: $doc_id})")[0] == {
        "doc_id": "d1"
    }


@pytest.mark.asyncio
async def test_prune_clauses_passes_keep_set():
    client = FakeGraphClient(returns={"RETURN pruned": [{"pruned": 2}]})
    pruned = await GraphWriter(client).prune_clauses("d1", ["a", "b"])
    assert pruned == 2
    params = client.queries_containing("WHERE NOT c.node_id IN $keep")[0]
    assert params == {"doc_id": "d1", "keep": ["a", "b"]}


@pytest.mark.asyncio
async def test_delete_restrictions_of_doc():
    client = FakeGraphClient(
        returns={"MATCH (r:Restriction {doc_id: $doc_id})": [{"deleted": 4}]}
    )
    deleted = await GraphWriter(client).delete_restrictions_of_doc("d1")
    assert deleted == 4


@pytest.mark.asyncio
async def test_stored_documents_projection():
    rows = [{"doc_id": "d1", "name": "A", "content_hash": "h"}]
    client = FakeGraphClient(returns={"MATCH (d:Document)": rows})
    assert await GraphWriter(client).stored_documents() == rows


def _detail() -> DocumentDetail:
    return DocumentDetail(
        doc_id="d1",
        name="СП 42",
        fragments=[
            DocumentFragment(id="a", order=0, text="keep"),
            DocumentFragment(id="b", order=1, text="also"),
        ],
    )


class FakeDVD:
    async def get_document(self, doc_id):
        return _detail()


@pytest.mark.asyncio
async def test_ingest_replace_prunes_stale_clauses():
    client = FakeGraphClient(returns={"RETURN pruned": [{"pruned": 1}]})
    svc = IngestionService(FakeDVD(), GraphWriter(client))

    result = await svc.ingest_document("d1", replace=True)

    prune = client.queries_containing("WHERE NOT c.node_id IN $keep")
    assert prune and prune[0]["keep"] == ["a", "b"]
    assert result.pruned_clauses == 1


@pytest.mark.asyncio
async def test_ingest_without_replace_does_not_prune():
    client = FakeGraphClient()
    await IngestionService(FakeDVD(), GraphWriter(client)).ingest_document("d1")
    assert client.queries_containing("WHERE NOT c.node_id IN $keep") == []


class _ReplaceWriter:
    """Minimal extraction-side writer that records the pre-extract restriction wipe."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_restrictions_of_doc(self, doc_id):
        self.deleted.append(doc_id)
        return 0

    async def get_clauses(self, doc_id):
        return []  # no clauses → extraction returns early after the wipe


@pytest.mark.asyncio
async def test_extract_replace_wipes_restrictions_first():
    writer = _ReplaceWriter()
    svc = ExtractionService(
        writer, extractor=None, kinds=None, entities=None, embedder=None
    )

    result = await svc.extract_document("d1", replace=True)

    assert writer.deleted == [
        "d1"
    ]  # stale restrictions dropped even when nothing re-extracts
    assert result.skipped is True


def test_extracted_restriction_importable():
    # Guard the extraction model import used above stays valid.
    assert ExtractedRestriction(subject="s", object="o", kind="k")

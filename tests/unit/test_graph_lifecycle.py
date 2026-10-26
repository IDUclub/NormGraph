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
async def test_upsert_restriction_reattaches_all_saved_plan_revisions():
    client = FakeGraphClient()

    await GraphWriter(client).upsert_restriction(
        {"id": "r1", "doc_id": "d1"},
        clause_node_id="c1",
        subject_normalized="source",
        object_normalized="target",
        kind_name="distance",
    )

    query = client.calls[0][0]
    assert "OPTIONAL MATCH (saved:CheckPlan {restriction_id: $id})" in query
    assert "restriction_id: $id, current: true" not in query


@pytest.mark.asyncio
async def test_append_check_plan_revision_can_skip_an_existing_current_plan():
    client = FakeGraphClient()

    await GraphWriter(client).append_check_plan_revision(
        "r1",
        {
            "schema_version": "1.0",
            "template": "unsupported",
            "template_version": 1,
            "params": {},
            "source": {"restriction_id": "r1"},
            "planner_status": "unsupported",
        },
        review_status="rejected",
        skip_if_current=True,
    )

    query, params = client.calls[0]
    assert "NOT $skip_if_current OR current IS NULL" in query
    assert params["skip_if_current"] is True


@pytest.mark.asyncio
async def test_check_plan_revision_stores_normalized_layer_entities():
    client = FakeGraphClient()

    await GraphWriter(client).append_check_plan_revision(
        "r1",
        {
            "schema_version": "1.0",
            "template": "distance_from_source",
            "template_version": 1,
            "params": {},
            "declared_requirements": {
                "layers": [
                    {"role": "schools", "entity": "Школа"},
                    {"role": "homes", "entity": "Жилой дом"},
                ]
            },
            "source": {"restriction_id": "r1"},
            "planner_status": "auto",
        },
        review_status="pending",
    )

    query, params = client.calls[0]
    assert "layer_entities: $layer_entities" in query
    assert params["layer_entities"] == ["жилой дом", "школа"]


@pytest.mark.asyncio
async def test_layer_entity_backfill_keys_every_unkeyed_plan_once():
    class PagedClient(FakeGraphClient):
        def __init__(self) -> None:
            super().__init__()
            self.pages = [
                [
                    {
                        "element_id": "e1",
                        "requirements_json": '{"layers": [{"entity": "Школа"}]}',
                    },
                    {"element_id": "e2", "requirements_json": "not json"},
                ],
                [],
            ]

        async def run(self, query: str, **params):
            self.calls.append((query, params))
            if "cp.layer_entities IS NULL" in query:
                return self.pages.pop(0)
            return []

    client = PagedClient()

    assert await GraphWriter(client).backfill_check_plan_layer_entities() == 2

    [update] = client.queries_containing("SET cp.layer_entities")
    assert update["rows"] == [
        {"element_id": "e1", "layer_entities": ["школа"]},
        {"element_id": "e2", "layer_entities": []},
    ]


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


@pytest.mark.asyncio
async def test_a_new_edition_takes_over_the_extraction_of_identical_clauses():
    """IDU_DVD rebuilds a consolidated edition as new fragments, most of them unchanged."""
    from src.pipeline.reuse import extraction_hash

    inventory = [
        # old edition: one clause amended, one unchanged, one never extracted
        {
            "node_id": "old-1",
            "text": "Высота не более 15 м",
            "char_start": 10,
            "extracted_hash": extraction_hash("Высота не более 15 м"),
        },
        {
            "node_id": "old-2",
            "text": "Отступ  3 м",
            "char_start": 40,
            "extracted_hash": extraction_hash("Отступ 3 м"),
        },
        {
            "node_id": "old-3",
            "text": "Без нормы",
            "char_start": 60,
            "extracted_hash": None,
        },
        # new edition
        {"node_id": "new-1", "text": "Высота не более 20 м", "char_start": 10},
        {"node_id": "new-2", "text": "Отступ 3 м", "char_start": 45},
        {"node_id": "new-3", "text": "Без нормы", "char_start": 65},
    ]
    client = FakeGraphClient(returns={"c.extracted_hash AS extracted_hash": inventory})

    carried = await GraphWriter(client).carry_unchanged_clauses(
        "d1", ["new-1", "new-2", "new-3"], extraction_hash
    )

    assert carried == ["new-2"]
    [move] = client.queries_containing("MERGE (r)-[:DERIVED_FROM]->(new)")
    assert move["moves"] == [
        {
            "old": "old-2",
            "new": "new-2",
            "hash": extraction_hash("Отступ 3 м"),
            "shift": 5,
        }
    ]


@pytest.mark.asyncio
async def test_ingest_carries_before_pruning():
    client = FakeGraphClient(returns={"RETURN pruned": [{"pruned": 1}]})
    svc = IngestionService(FakeDVD(), GraphWriter(client))

    result = await svc.ingest_document("d1", replace=True)

    queries = [q for q, _ in client.calls]
    inventory = next(
        i for i, q in enumerate(queries) if "c.extracted_hash AS extracted_hash" in q
    )
    prune = next(
        i for i, q in enumerate(queries) if "WHERE NOT c.node_id IN $keep" in q
    )
    assert inventory < prune
    assert result.carried_clauses == 0


@pytest.mark.asyncio
async def test_clauses_record_the_acts_that_amended_them():
    detail = DocumentDetail(
        doc_id="d1",
        name="ПЗЗ",
        fragments=[
            DocumentFragment(id="a", text="x", amended_by=["Приказ № 170"]),
            DocumentFragment(id="b", text="y"),
        ],
    )
    props = [IngestionService._clause_props(detail, f) for f in detail.fragments]
    assert props[0]["amended_by"] == ["Приказ № 170"]
    assert props[1]["amended_by"] == []  # cleared when no act touches it any more

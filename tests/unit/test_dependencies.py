"""Semantic relations from IDU_DVD mirrored as DEPENDS_ON edges."""

from __future__ import annotations

import pytest
from _fakes import FakeWriter

from src.dvd_client.models import DocumentDetail, DocumentFragment, FragmentRelation
from src.ingestion.service import IngestionService

LEAD_IN = "Ширину тротуаров на улицах местного значения следует принимать:"
ITEM = "в жилой застройке — не менее 2,25 м;"


class FakeDVD:
    def __init__(self, relations):
        self.relations = relations

    async def get_document(self, doc_id):
        frags = [
            DocumentFragment(id="lead", text=LEAD_IN, order=0),
            DocumentFragment(id="item", text=ITEM, parent_id="lead", order=1),
        ]
        return DocumentDetail(doc_id=doc_id, name="СП", version="1", fragments=frags)

    async def get_relations(self, doc_id, min_weight=0.0):
        return self.relations


class GraphRecorder(FakeWriter):
    async def upsert_clause(self, props):
        self._rec("upsert_clause", **props)

    async def link_part_of(self, child, parent):
        self._rec("link_part_of", child=child, parent=parent)

    async def link_reference(self, src, ref):
        self._rec("link_reference", src=src)

    async def replace_dependencies(self, doc_id, rows):
        self._rec("replace_dependencies", doc_id=doc_id, rows=rows)
        return len(rows)


@pytest.mark.asyncio
async def test_ingestion_mirrors_relations_of_this_version_only():
    relations = [
        FragmentRelation(
            source_id="lead", target_id="item", weight=1.0, kind="completes"
        ),
        FragmentRelation(
            source_id="lead", target_id="old-version-node", weight=0.9, kind="refines"
        ),
    ]
    writer = GraphRecorder()
    result = await IngestionService(FakeDVD(relations), writer).ingest_document("d1")
    (call,) = writer.named("replace_dependencies")
    assert call["rows"] == [
        {"source": "lead", "target": "item", "weight": 1.0, "kind": "completes"}
    ]
    assert result.dependencies == 1


@pytest.mark.asyncio
async def test_ingestion_survives_a_dvd_without_relations():
    class Failing(FakeDVD):
        async def get_relations(self, doc_id, min_weight=0.0):
            raise RuntimeError("down")

    writer = GraphRecorder()
    result = await IngestionService(Failing([]), writer).ingest_document("d1")
    assert result.clauses == 2 and result.dependencies == 0

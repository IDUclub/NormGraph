"""Query service: row mapping, neighbours, graph BFS, applicable, DVD fallback."""

from __future__ import annotations

import pytest
from _fakes import FakeEmbedder

from src.common.config import Settings
from src.dto.query import ApplicableRequest, RestrictionSearchRequest
from src.dvd_client.models import SearchHit, SearchResponse
from src.query.service import QueryService


def _row(rid, subject="СЗЗ", obj="жилье", kind="запрет_размещения", score=None, **kw):
    row = {
        "id": rid,
        "subject": subject,
        "object": obj,
        "kind": kind,
        "kind_status": "approved",
        "extraction_text": "не допускается",
        "value_operator": None,
        "value_number": None,
        "value_unit": None,
        "value_condition": None,
        "score": score,
        "subject_normalized": "сзз",
        "object_normalized": "жилье",
        "clause_node_id": "c1",
        "numbering": "8.3",
        "breadcrumb": "СП / 8 / 8.3",
        "tags": ["зонирование"],
        "char_start": 100,
        "char_end": 130,
        "doc_id": "d1",
        "name": "СП 42",
        "version": "2016",
        "version_id": "v1",
        "doc_type": "regulation",
        "corpus": "norms",
        "lang": "ru",
    }
    row.update(kw)
    return row


class FakeReader:
    def __init__(self):
        self.rows = {}
        self.adj = {}  # id -> list[(neighbor_id, relation)]
        self.vector_rows = []
        self.filter_rows = []
        self.applicable_rows = []
        self.nearest = []
        self.entities = []
        self.kinds = []

    async def search_vector(self, index, embedding, filters, *, limit, oversample=5):
        return self.vector_rows[:limit]

    async def search_filter(self, filters, *, limit):
        return self.filter_rows[:limit]

    async def get_by_ids(self, ids):
        return [self.rows[i] for i in ids if i in self.rows]

    async def applicable(self, targets, filters, *, limit):
        self.last_targets = targets
        return self.applicable_rows[:limit]

    async def nearest_entities(self, index, embedding, *, k=5):
        return self.nearest

    async def neighbors(self, ids):
        out = []
        for i in ids:
            for nb, rel in self.adj.get(i, []):
                out.append({"src": i, "neighbor_id": nb, "relation": rel})
        return out

    async def list_entities(self, query, *, limit):
        return self.entities[:limit]

    async def list_kinds(self):
        return self.kinds


class FakeDVD:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, query, **kw):
        return SearchResponse(count=len(self._hits), hits=self._hits)


def _svc(reader, dvd=None):
    return QueryService(reader, FakeEmbedder(), dvd or FakeDVD([]), Settings())


@pytest.mark.asyncio
async def test_search_vector_maps_rows():
    reader = FakeReader()
    reader.vector_rows = [_row("r1", score=0.7)]
    resp = await _svc(reader).search(RestrictionSearchRequest(query="СЗЗ"))
    assert resp.count == 1
    hit = resp.hits[0]
    assert hit.id == "r1" and hit.score == 0.7
    assert hit.provenance.numbering == "8.3" and hit.provenance.doc_id == "d1"
    assert hit.tags == ["зонирование"]


@pytest.mark.asyncio
async def test_search_filter_when_no_query():
    reader = FakeReader()
    reader.filter_rows = [_row("r1"), _row("r2")]
    resp = await _svc(reader).search(RestrictionSearchRequest(kind="запрет_размещения"))
    assert resp.count == 2 and resp.hits[0].score is None


@pytest.mark.asyncio
async def test_dvd_fallback_when_graph_empty():
    reader = FakeReader()  # no vector rows
    dvd = FakeDVD(
        [SearchHit(id="x", doc_id="d9", name="СП 99", numbering="4.1", text="...")]
    )
    resp = await _svc(reader, dvd).search(RestrictionSearchRequest(query="ничего"))
    assert resp.count == 0
    assert resp.dvd_fallback and resp.dvd_fallback[0].name == "СП 99"


@pytest.mark.asyncio
async def test_get_returns_detail_with_neighbors():
    reader = FakeReader()
    reader.rows = {"r1": _row("r1"), "r2": _row("r2")}
    reader.adj = {"r1": [("r2", "shares_entity")]}
    detail = await _svc(reader).get("r1")
    assert detail.id == "r1"
    assert len(detail.neighbors) == 1
    assert detail.neighbors[0].relation == "shares_entity"
    assert detail.neighbors[0].restriction.id == "r2"


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    assert await _svc(FakeReader()).get("nope") is None


@pytest.mark.asyncio
async def test_graph_bfs_depth_two():
    reader = FakeReader()
    reader.rows = {k: _row(k) for k in ("r1", "r2", "r3")}
    reader.adj = {
        "r1": [("r2", "shares_entity")],
        "r2": [("r3", "reference")],
    }
    graph = await _svc(reader).graph("r1", depth=2)
    assert {n.id for n in graph.nodes} == {"r1", "r2", "r3"}
    rels = {(e.source, e.target, e.relation) for e in graph.edges}
    assert ("r1", "r2", "shares_entity") in rels
    assert ("r2", "r3", "reference") in rels


@pytest.mark.asyncio
async def test_applicable_resolves_targets_and_returns_hits():
    reader = FakeReader()
    reader.nearest = [{"normalized": "жилая застройка", "score": 0.95}]
    reader.applicable_rows = [_row("r1")]
    resp = await _svc(reader).applicable(ApplicableRequest(object="жилье"))
    assert resp.count == 1
    # exact-normalized object + the fuzzy neighbour above threshold are both queried
    assert "жилье" in reader.last_targets
    assert "жилая застройка" in reader.last_targets


@pytest.mark.asyncio
async def test_search_with_neighbors_depth_attaches_neighbors():
    reader = FakeReader()
    reader.vector_rows = [_row("r1", score=0.9)]
    reader.rows = {"r2": _row("r2")}
    reader.adj = {"r1": [("r2", "shares_entity")]}
    resp = await _svc(reader).search(
        RestrictionSearchRequest(query="x", neighbors_depth=1)
    )
    assert [n.restriction.id for n in resp.neighbors] == ["r2"]

"""Query service: row mapping, neighbours, graph BFS, applicable, DVD fallback."""

from __future__ import annotations

import pydantic
import pytest
from _fakes import FakeEmbedder

from src.common.config import Settings
from src.dto.query import (
    ApplicableRequest,
    DocumentListRequest,
    EntityResolveRequest,
    RestrictionListRequest,
    RestrictionSearchRequest,
)
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
        self.conflict_rows = []
        self.entity_aliases = {}  # normalized -> aliases
        self.text_candidates = []
        self.layer_candidates = []
        self.details = {}  # normalized -> entity row
        self.document_rows = []

    async def search_vector(self, index, embedding, filters, *, limit, oversample=5):
        return self.vector_rows[:limit]

    async def search_filter(self, filters, *, limit):
        self.last_filters = filters
        return self.filter_rows[:limit]

    async def list_page(self, filters, *, after_id, limit, executable_only=False):
        self.last_page_args = (filters, after_id, limit, executable_only)
        rows = [r for r in self.filter_rows if after_id is None or r["id"] > after_id]
        return rows[:limit]

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

    async def entity_keys(self, names):
        keys = set(names)
        for normalized, aliases in self.entity_aliases.items():
            if normalized in names or set(aliases) & set(names):
                keys.add(normalized)
                keys.update(aliases)
        return sorted(keys)

    async def entity_candidates_by_text(self, term, stems, *, limit):
        self.last_text_lookup = (term, stems)
        return self.text_candidates[:limit]

    async def layer_entity_candidates(self, term, stems, *, limit):
        self.last_layer_lookup = (term, stems)
        return self.layer_candidates[:limit]

    async def entity_details(self, names):
        return [self.details[n] for n in names if n in self.details]

    async def list_documents(self, filters, *, executable_only, limit):
        self.last_document_args = (filters, executable_only, limit)
        return self.document_rows[:limit]

    async def conflict_pairs(
        self, *, user_id=None, scenario_id=None, restriction_id=None, limit=50
    ):
        self.last_conflict_scope = (user_id, scenario_id, restriction_id)
        return self.conflict_rows[:limit]


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
async def test_list_page_walks_the_keyset_until_exhausted():
    reader = FakeReader()
    reader.filter_rows = [_row(f"r{i}") for i in range(1, 6)]
    svc = _svc(reader)

    seen, after_id = [], None
    while True:
        page = await svc.list_page(
            RestrictionListRequest(after_id=after_id, limit=2, executable_only=True)
        )
        seen.extend(hit.id for hit in page.hits)
        if page.next_after_id is None:
            break
        after_id = page.next_after_id

    assert seen == ["r1", "r2", "r3", "r4", "r5"]
    assert reader.last_page_args[2:] == (3, True)


@pytest.mark.asyncio
async def test_list_page_exact_fit_has_no_next_page():
    reader = FakeReader()
    reader.filter_rows = [_row("r1"), _row("r2")]

    page = await _svc(reader).list_page(RestrictionListRequest(limit=2))

    assert page.count == 2 and page.next_after_id is None


@pytest.mark.parametrize(
    "request_type", [RestrictionSearchRequest, RestrictionListRequest]
)
def test_page_size_is_capped(request_type):
    with pytest.raises(pydantic.ValidationError):
        request_type(limit=501)


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
async def test_applicable_uses_the_query_threshold_not_the_merge_threshold():
    reader = FakeReader()
    # measured Giga similarities: a synonym below the 0.90 merge threshold must still
    # resolve, an unrelated facility must not
    reader.nearest = [
        {"normalized": "общеобразовательные организации", "score": 0.80},
        {"normalized": "детские сады", "score": 0.68},
    ]
    await _svc(reader).applicable(ApplicableRequest(object="школы"))
    assert set(reader.last_targets) == {"школы", "общеобразовательные организации"}


@pytest.mark.asyncio
async def test_kinds_filter_reaches_the_reader():
    reader = FakeReader()
    await _svc(reader).search(
        RestrictionSearchRequest(kinds=["минимальное_расстояние", "запрет_размещения"])
    )
    assert reader.last_filters["kinds"] == [
        "минимальное_расстояние",
        "запрет_размещения",
    ]


@pytest.mark.asyncio
async def test_list_conflicts_resolves_pairs_to_full_rows():
    reader = FakeReader()
    reader.rows = {"r1": _row("r1"), "r2": _row("r2")}
    reader.conflict_rows = [
        {
            "restriction_id": "r1",
            "other_id": "r2",
            "reason": "incompatible bounds",
            "severity": "certain",
        }
    ]
    resp = await _svc(reader).list_conflicts(user_id="u1", scenario_id="s1")

    assert resp.count == 1
    assert resp.conflicts[0].restriction.id == "r1"
    assert resp.conflicts[0].other.id == "r2"
    assert resp.conflicts[0].severity == "certain"
    assert reader.last_conflict_scope == ("u1", "s1", None)


@pytest.mark.asyncio
async def test_list_conflicts_empty_when_no_pairs():
    resp = await _svc(FakeReader()).list_conflicts()
    assert resp.count == 0 and resp.conflicts == []


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


def _entity(normalized, executable=0, restrictions=1, aliases=()):
    return {
        "normalized": normalized,
        "name": normalized,
        "aliases": list(aliases),
        "status": "active",
        "restriction_count": restrictions,
        "executable_count": executable,
    }


@pytest.mark.asyncio
async def test_topic_entities_are_normalized_and_expanded_to_aliases():
    reader = FakeReader()
    reader.entity_aliases = {"школа": ["школа", "школы"]}

    await _svc(reader).list_page(RestrictionListRequest(entities=["  Школы "]))

    assert reader.last_page_args[0]["entities"] == ["школа", "школы"]


@pytest.mark.asyncio
async def test_no_topic_leaves_the_entity_filter_unbound():
    reader = FakeReader()

    await _svc(reader).search(RestrictionSearchRequest(kind="запрет_размещения"))

    assert reader.last_filters["entities"] is None


@pytest.mark.asyncio
async def test_resolve_entities_labels_text_matches_and_appends_vector_ones():
    reader = FakeReader()
    reader.text_candidates = [
        _entity("школа", executable=3, aliases=["школы"]),
        _entity("спортивная школа"),
    ]
    reader.nearest = [
        {"normalized": "школа", "score": 0.99},
        {"normalized": "общеобразовательная организация", "score": 0.81},
    ]
    reader.details = {
        "общеобразовательная организация": _entity(
            "общеобразовательная организация", executable=2
        )
    }

    [resolution] = await _svc(reader).resolve_entities(
        EntityResolveRequest(terms=["Школы"])
    )

    assert reader.last_text_lookup == ("школы", ["школ"])
    assert [(c.normalized, c.match) for c in resolution.candidates] == [
        ("школа", "alias"),
        ("спортивная школа", "text"),
        ("общеобразовательная организация", "vector"),
    ]
    assert resolution.candidates[0].executable_count == 3
    assert resolution.candidates[2].score == 0.81


@pytest.mark.asyncio
async def test_resolve_entities_offers_plan_layers_that_are_not_entities():
    reader = FakeReader()
    reader.text_candidates = [_entity("детский сад-ясли")]
    reader.layer_candidates = [
        {"normalized": "детский сад", "restriction_count": 4, "executable_count": 3},
        {
            "normalized": "детский сад-ясли",
            "restriction_count": 1,
            "executable_count": 1,
        },
        {"normalized": "детские сады и школы", "restriction_count": 2},
    ]

    [resolution] = await _svc(reader).resolve_entities(
        EntityResolveRequest(terms=["детские сады"])
    )

    assert reader.last_layer_lookup == reader.last_text_lookup
    assert [(c.normalized, c.match) for c in resolution.candidates] == [
        ("детский сад-ясли", "text"),
        ("детский сад", "layer_text"),
        ("детские сады и школы", "layer_text"),
    ]
    assert resolution.candidates[1].executable_count == 3


@pytest.mark.asyncio
async def test_resolve_entities_marks_a_layer_named_exactly_like_the_topic():
    reader = FakeReader()
    reader.layer_candidates = [
        {"normalized": "детский сад", "restriction_count": 4, "executable_count": 3}
    ]

    [resolution] = await _svc(reader).resolve_entities(
        EntityResolveRequest(terms=["детский сад"])
    )

    assert [(c.normalized, c.match) for c in resolution.candidates] == [
        ("детский сад", "layer")
    ]


@pytest.mark.asyncio
async def test_resolve_entities_keeps_text_matches_when_embedding_fails():
    class BrokenEmbedder(FakeEmbedder):
        async def embed_documents(self, texts):
            raise RuntimeError("embeddings down")

    reader = FakeReader()
    reader.text_candidates = [_entity("жилой дом")]
    svc = QueryService(reader, BrokenEmbedder(), FakeDVD([]), Settings())

    [resolution] = await svc.resolve_entities(EntityResolveRequest(terms=["жилой дом"]))

    assert [(c.normalized, c.match) for c in resolution.candidates] == [
        ("жилой дом", "exact")
    ]


@pytest.mark.asyncio
async def test_list_documents_passes_expanded_filters_and_maps_counts():
    reader = FakeReader()
    reader.entity_aliases = {"школа": ["школы"]}
    reader.document_rows = [
        {
            "doc_id": "d1",
            "name": "СП 42.13330.2016",
            "version": "2016",
            "version_id": "v1",
            "doc_type": "regulation",
            "corpus": "norms",
            "restriction_count": 5,
            "executable_count": 2,
        }
    ]

    response = await _svc(reader).list_documents(
        DocumentListRequest(entities=["школа"], executable_only=True, limit=10)
    )

    filters, executable_only, limit = reader.last_document_args
    assert filters["entities"] == ["школа", "школы"]
    assert (executable_only, limit) == (True, 10)
    assert response.count == 1
    assert response.documents[0].executable_count == 2

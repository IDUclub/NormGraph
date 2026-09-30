"""DVD client parsing — hermetic, HTTP mocked with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.dvd_client import DVDClient


class FakeServiceAuth:
    async def get_authorization_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer service-token"}


def _client() -> DVDClient:
    return DVDClient("http://dvd.test", FakeServiceAuth())


@pytest.mark.asyncio
@respx.mock
async def test_get_document_parses_fragments_and_references():
    respx.get("http://dvd.test/library/documents/d1").mock(
        return_value=httpx.Response(
            200,
            json={
                "doc_id": "d1",
                "name": "СП 42.13330.2016",
                "version": "2016",
                "version_id": "v1",
                "fragments": [
                    {"id": "a", "order": 0, "numbering": "8", "text": "root"},
                    {
                        "id": "b",
                        "order": 1,
                        "numbering": "8.3",
                        "parent_id": "a",
                        "text": "clause",
                        "references": [
                            {
                                "raw": "СП 52.13330",
                                "target_name": "СП 52.13330",
                                "target_node_id": "x",
                                "scope": "external",
                                "resolved": True,
                            }
                        ],
                    },
                ],
            },
        )
    )
    client = _client()
    detail = await client.get_document("d1")
    await client.aclose()

    assert detail is not None
    assert detail.doc_id == "d1"
    assert len(detail.fragments) == 2
    frag_b = detail.fragments[1]
    assert frag_b.parent_id == "a"
    assert frag_b.references[0].resolved is True
    assert frag_b.references[0].target_node_id == "x"


@pytest.mark.asyncio
@respx.mock
async def test_get_document_returns_none_on_404():
    respx.get("http://dvd.test/library/documents/missing").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    client = _client()
    assert await client.get_document("missing") is None
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_resolve_doc_ids_uses_lookup():
    respx.get("http://dvd.test/library/lookup").mock(
        return_value=httpx.Response(
            200,
            json={"count": 1, "documents": [{"doc_id": "d1", "name": "СП 42"}]},
        )
    )
    client = _client()
    ids = await client.resolve_doc_ids("СП 42")
    await client.aclose()
    assert ids == ["d1"]


@pytest.mark.asyncio
@respx.mock
async def test_resolve_user_doc_ids_queries_scoped_endpoint():
    route = respx.get("http://dvd.test/user-documents").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "documents": [{"doc_id": "ud1", "name": "мой документ"}],
            },
        )
    )
    client = _client()
    ids = await client.resolve_user_doc_ids("u1", "s1", "мой документ")
    await client.aclose()

    assert ids == ["ud1"]
    sent = route.calls.last.request.url.params
    assert "user_id" not in sent
    assert sent["scenario_id"] == "s1"
    assert sent["name"] == "мой документ"
    assert sent["include_inherited"] == "false"
    assert route.calls.last.request.headers["X-User-Id"] == "u1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer service-token"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_user_doc_ids_returns_empty_on_404():
    respx.get("http://dvd.test/user-documents").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    client = _client()
    ids = await client.resolve_user_doc_ids("u1", "s1", "nope")
    await client.aclose()
    assert ids == []


@pytest.mark.asyncio
@respx.mock
async def test_get_relations_parses_edges_and_tolerates_an_older_dvd():
    route = respx.get("http://dvd.test/library/documents/d1/relations").mock(
        return_value=httpx.Response(
            200,
            json={
                "doc_id": "d1",
                "relations": [
                    {
                        "source_id": "lead",
                        "target_id": "item",
                        "doc_id": "d1",
                        "weight": 1.0,
                        "kind": "completes",
                        "method": "heuristic",
                    }
                ],
            },
        )
    )
    respx.get("http://dvd.test/library/documents/old/relations").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"})
    )
    client = _client()
    (edge,) = await client.get_relations("d1", min_weight=0.5)
    missing = await client.get_relations("old")
    await client.aclose()

    assert (edge.source_id, edge.target_id, edge.kind) == ("lead", "item", "completes")
    assert route.calls[0].request.url.params["min_weight"] == "0.5"
    assert missing == []

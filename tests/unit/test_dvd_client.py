"""DVD client parsing — hermetic, HTTP mocked with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.dvd_client import DVDClient


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
    client = DVDClient("http://dvd.test")
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
    client = DVDClient("http://dvd.test")
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
    client = DVDClient("http://dvd.test")
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
    client = DVDClient("http://dvd.test")
    ids = await client.resolve_user_doc_ids("u1", "s1", "мой документ")
    await client.aclose()

    assert ids == ["ud1"]
    sent = route.calls.last.request.url.params
    assert sent["user_id"] == "u1"
    assert sent["scenario_id"] == "s1"
    assert sent["name"] == "мой документ"
    assert sent["include_inherited"] == "false"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_user_doc_ids_returns_empty_on_404():
    respx.get("http://dvd.test/user-documents").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    client = DVDClient("http://dvd.test")
    ids = await client.resolve_user_doc_ids("u1", "s1", "nope")
    await client.aclose()
    assert ids == []

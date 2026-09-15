"""Provider unit tests — hermetic, HTTP mocked with respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from src.common.config import Settings
from src.providers import build_embedder, build_llm
from src.providers.embeddings_openai import OpenAICompatibleEmbedder
from src.providers.llm_openai import OpenAICompatibleLLM


@respx.mock
def test_openai_llm_complete_sync():
    route = respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}]}
        )
    )
    llm = OpenAICompatibleLLM("http://llm.test/v1", "m", api_key="k")
    assert llm.complete_sync("hi", system="s") == "hello"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer k"


@pytest.mark.parametrize("content", [None, "", "   "])
@respx.mock
def test_openai_empty_final_is_not_an_empty_extraction(content):
    respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}]
            },
        )
    )
    llm = OpenAICompatibleLLM("http://llm.test/v1", "gpt-oss-20b")
    with pytest.raises(ValueError, match="no final answer"):
        llm.complete_sync("Extract restrictions")


@pytest.mark.asyncio
@respx.mock
async def test_openai_rejects_truncated_json_and_bounds_gpt_oss_reasoning():
    route = respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": "[]"}}]
            },
        )
    )
    llm = OpenAICompatibleLLM("http://llm.test/v1", "gpt-oss-20b")
    try:
        with pytest.raises(ValueError, match="did not finish"):
            await llm.complete("Extract restrictions")
        assert json.loads(route.calls.last.request.content)["reasoning_effort"] == "low"
    finally:
        await llm.aclose()


@respx.mock
def test_openai_embedder_documents_sync_orders_by_index():
    respx.post("http://emb.test/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )
    )
    emb = OpenAICompatibleEmbedder("http://emb.test", "giga", dim=2)
    vectors = emb.embed_documents_sync(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.asyncio
@respx.mock
async def test_openai_embedder_query_applies_prompt():
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    respx.post("http://emb.test/v1/embeddings").mock(side_effect=_handler)
    emb = OpenAICompatibleEmbedder("http://emb.test", "giga", dim=1, query_prompt="Q: ")
    await emb.embed_query("what")
    assert captured["prompt"] == "Q: "


def test_factory_selects_providers():
    s = Settings(
        llm_provider="openai_compatible",
        embeddings_provider="ollama",
        vector_size=1024,
    )
    llm = build_llm(s)
    emb = build_embedder(s)
    assert isinstance(llm, OpenAICompatibleLLM)
    assert emb.dim == 1024


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError):
        build_llm(Settings(llm_provider="nope"))

"""Provider unit tests — hermetic, HTTP mocked with respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from src.common.config import Settings
from src.providers import build_embedder, build_llm
from src.providers.embeddings_openai import OpenAICompatibleEmbedder
from src.providers.llm_ollama import OllamaLLM
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


@pytest.mark.parametrize("effort", [None, "low"])
@respx.mock
def test_openai_llm_sends_reasoning_effort_only_when_configured(effort):
    route = respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )
    )
    OpenAICompatibleLLM(
        "http://llm.test/v1", "m", reasoning_effort=effort
    ).complete_sync("hi")
    payload = json.loads(route.calls.last.request.content)
    assert payload.get("reasoning_effort") == effort


@respx.mock
def test_openai_llm_returns_empty_content_of_a_truncated_answer():
    respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": None}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 4096},
            },
        )
    )
    assert OpenAICompatibleLLM("http://llm.test/v1", "m").complete_sync("hi") == ""


def _chat(content, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": content}, "finish_reason": finish_reason}
            ]
        },
    )


def _sent_budgets(route):
    return [json.loads(c.request.content)["max_tokens"] for c in route.calls]


@respx.mock
def test_openai_llm_regrows_the_window_of_a_truncated_answer():
    route = respx.post("http://llm.test/v1/chat/completions").mock(
        side_effect=[_chat('{"extr', "length"), _chat('{"extractions', "length")]
        + [_chat('{"extractions": []}')]
    )
    llm = OpenAICompatibleLLM(
        "http://llm.test/v1", "m", max_tokens=1000, max_tokens_limit=8000
    )
    assert llm.complete_sync("hi") == '{"extractions": []}'
    assert _sent_budgets(route) == [1000, 2000, 4000]


@pytest.mark.asyncio
@respx.mock
async def test_openai_llm_window_stops_growing_at_the_limit():
    route = respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=_chat("cut", "length")
    )
    llm = OpenAICompatibleLLM(
        "http://llm.test/v1", "m", max_tokens=1000, max_tokens_limit=3000
    )
    assert await llm.complete("hi", max_tokens=500) == "cut"
    assert _sent_budgets(route) == [500, 1000, 2000, 3000]


@respx.mock
def test_openai_llm_keeps_the_truncated_answer_when_the_grown_window_is_rejected():
    route = respx.post("http://llm.test/v1/chat/completions").mock(
        side_effect=[
            _chat("partial", "length"),
            httpx.Response(400, json={"error": "maximum context length"}),
        ]
    )
    llm = OpenAICompatibleLLM(
        "http://llm.test/v1", "m", max_tokens=1000, max_tokens_limit=8000
    )
    assert llm.complete_sync("hi") == "partial"
    assert _sent_budgets(route) == [1000, 2000]


@respx.mock
def test_openai_llm_first_request_error_is_not_swallowed():
    respx.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": "bad"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        OpenAICompatibleLLM("http://llm.test/v1", "m").complete_sync("hi")


@respx.mock
def test_ollama_llm_regrows_the_window_of_a_truncated_answer():
    route = respx.post("http://ollama.test/api/chat").mock(
        side_effect=[
            httpx.Response(
                200, json={"message": {"content": "cu"}, "done_reason": "length"}
            ),
            httpx.Response(
                200, json={"message": {"content": "full"}, "done_reason": "stop"}
            ),
        ]
    )
    llm = OllamaLLM("http://ollama.test", "m", max_tokens=1000, max_tokens_limit=4000)
    assert llm.complete_sync("hi") == "full"
    assert [
        json.loads(c.request.content)["options"]["num_predict"] for c in route.calls
    ] == [1000, 2000]


def test_build_llm_passes_the_window_limit():
    llm = build_llm(
        Settings(_env_file=None, llm_max_tokens=4096, llm_max_tokens_limit=16384)
    )
    assert (llm._max_tokens, llm._max_tokens_limit) == (4096, 16384)


def test_build_llm_passes_reasoning_effort():
    llm = build_llm(Settings(_env_file=None, llm_reasoning_effort="low"))
    assert llm._reasoning_effort == "low"


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

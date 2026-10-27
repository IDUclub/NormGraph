"""Request priorities are explicit, isolated per document and optional on the wire."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from src.common.config import Settings
from src.pipeline.extractor import RestrictionExtractor
from src.providers.factory import build_llm
from src.providers.langextract_backend import ProviderLanguageModel
from src.providers.llm_openai import OpenAICompatibleLLM
from src.providers.request_priority import RequestPriorities

URL = "http://llm.test/v1/chat/completions"
EMPTY = '{"extractions": []}'


def chat(text=EMPTY, finish="stop"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}, "finish_reason": finish}]},
    )


def payloads(route):
    return [json.loads(call.request.content) for call in route.calls]


def test_registry_defaults_and_overrides():
    defaults = RequestPriorities()
    assert defaults.resolve("extraction") == 100
    assert defaults.resolve("check_plans") == 100
    assert defaults.resolve("query") == -100
    for kind in ("extraction", "check_plans", "query"):
        assert defaults.resolve(kind, user_document=True) == 20
    configured = RequestPriorities(" extraction = 60, user=0,query=-120,extraction=70")
    assert configured.resolve("extraction") == 70
    assert configured.resolve("query") == -120
    assert configured.resolve("check_plans", user_document=True) == 0
    assert RequestPriorities(enabled=False).resolve("query") == 0
    with pytest.raises(ValueError, match="unknown"):
        defaults.resolve("missing")


def test_invalid_entries_warn_once_and_preserve_valid_entries():
    with capture_logs() as logs:
        priorities = RequestPriorities("bad=1,extraction=x,check_plans=7,user=,oops,")
    assert priorities.resolve("extraction") == 100
    assert priorities.resolve("check_plans") == 7
    assert priorities.resolve("query", user_document=True) == 20
    assert [item["event"] for item in logs] == ["llm_request_priorities_invalid"]


def test_settings_read_priority_environment(monkeypatch):
    monkeypatch.setenv("NG_LLM_REQUEST_PRIORITY_ENABLED", "false")
    monkeypatch.setenv("NG_LLM_REQUEST_PRIORITIES", "extraction=60,user=0")
    settings = Settings(_env_file=None)
    assert settings.llm_request_priority_enabled is False
    assert settings.llm_request_priorities == "extraction=60,user=0"


def test_call_kind_cannot_be_omitted():
    llm = OpenAICompatibleLLM("http://llm.test/v1", "m")
    with pytest.raises(TypeError, match="kind"):
        llm.complete_sync("unclassified")


@pytest.mark.parametrize("kind", ["extraction", "check_plans", "query"])
@pytest.mark.parametrize("user_document", [False, True])
@pytest.mark.parametrize(
    "enabled,overrides",
    [(True, ""), (False, ""), (True, "user=0,extraction=0,check_plans=0,query=0")],
)
@pytest.mark.asyncio
@respx.mock
async def test_priority_payload_sync_and_async(kind, user_document, enabled, overrides):
    route = respx.post(URL).mock(return_value=chat())
    llm = build_llm(
        Settings(
            _env_file=None,
            llm_base_url="http://llm.test/v1",
            llm_request_priority_enabled=enabled,
            llm_request_priorities=overrides,
        )
    )
    try:
        llm.complete_sync("sync", kind=kind, user_document=user_document)
        await llm.complete("async", kind=kind, user_document=user_document)
        for payload in payloads(route):
            if not enabled or overrides:
                assert "priority" not in payload
            else:
                assert payload["priority"] == (
                    20 if user_document else -100 if kind == "query" else 100
                )
    finally:
        await llm.aclose()


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize(
    "message", ["priority scheduling is not enabled", "priority is not supported"]
)
@pytest.mark.asyncio
@respx.mock
async def test_unsupported_priority_retries_once_and_disables_both_paths(
    async_call, message
):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": message}}),
            chat(),
            chat(),
            chat(),
        ]
    )
    llm = OpenAICompatibleLLM("http://llm.test/v1", "m")
    try:
        with capture_logs() as logs:
            if async_call:
                await llm.complete("first", kind="check_plans")
            else:
                llm.complete_sync("first", kind="extraction")
            await llm.complete("later", kind="query")
            llm.complete_sync("later sync", kind="extraction", user_document=True)
        sent = payloads(route)
        assert len(sent) == 4
        assert sent[0].pop("priority") == 100
        assert sent[0] == sent[1]
        assert all("priority" not in item for item in sent[1:])
        assert [item["event"] for item in logs] == ["llm_request_priority_unsupported"]
    finally:
        await llm.aclose()


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("retry_error", [False, True])
@pytest.mark.asyncio
@respx.mock
async def test_priority_rejection_on_grown_window_precedes_window_fallback(
    async_call, retry_error
):
    route = respx.post(URL).mock(
        side_effect=[
            chat("partial", "length"),
            httpx.Response(400, text="priority scheduling is not enabled"),
            (
                httpx.Response(400, text="maximum context length")
                if retry_error
                else chat("full")
            ),
        ]
    )
    llm = OpenAICompatibleLLM(
        "http://llm.test/v1", "m", max_tokens=10, max_tokens_limit=20
    )
    try:
        if async_call:
            result = await llm.complete("prompt", kind="check_plans")
        else:
            result = llm.complete_sync("prompt", kind="extraction")
        assert result == ("partial" if retry_error else "full")
        assert [item["max_tokens"] for item in payloads(route)] == [10, 20, 20]
        assert "priority" not in payloads(route)[-1]
    finally:
        await llm.aclose()


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
@respx.mock
async def test_priority_fallback_does_not_loop_or_swallow_second_priority_error(
    async_call,
):
    route = respx.post(URL).mock(
        side_effect=[
            chat("partial", "length"),
            httpx.Response(400, text="priority is not supported"),
            httpx.Response(400, text="priority is not supported"),
        ]
    )
    llm = OpenAICompatibleLLM(
        "http://llm.test/v1", "m", max_tokens=10, max_tokens_limit=20
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            if async_call:
                await llm.complete("prompt", kind="check_plans")
            else:
                llm.complete_sync("prompt", kind="extraction")
        assert len(route.calls) == 3
        assert "priority" not in payloads(route)[-1]
    finally:
        await llm.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_extractor_origin_survives_nested_workers_without_leaking(monkeypatch):
    route = respx.post(URL).mock(return_value=chat())
    llm = OpenAICompatibleLLM("http://llm.test/v1", "m")
    backend = ProviderLanguageModel(llm, model_id="m")
    extractor = RestrictionExtractor(backend)
    started = Barrier(2)
    llm._sync = httpx.Client()

    def extract_in_worker(**kwargs):
        started.wait(timeout=5)
        # langextract may call infer in a plain executor inside our to_thread worker.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(
                lambda: list(kwargs["model"].infer([kwargs["text_or_documents"]]))
            ).result()
        return SimpleNamespace(extractions=[])

    monkeypatch.setattr("src.pipeline.extractor.lx.extract", extract_in_worker)
    try:
        await asyncio.gather(
            extractor.extract_clause("user text", user_document=True),
            extractor.extract_clause("corpus text", user_document=False),
        )
        by_prompt = {
            item["messages"][-1]["content"]: item["priority"]
            for item in payloads(route)
        }
        assert by_prompt == {"user text": 20, "corpus text": 100}
        list(backend.infer(["original backend"]))
        assert payloads(route)[-1]["priority"] == 100
    finally:
        await llm.aclose()

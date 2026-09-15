"""Recovery pages remain resumable when documents fail or produce no norms."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from src.dto.extraction import ExtractionBackfillRequest
from src.pipeline.router import extraction_router
from src.pipeline.service import ExtractionService, ExtractResult


def service(ids):
    writer = SimpleNamespace(
        documents_without_restrictions=AsyncMock(
            return_value=[{"doc_id": doc_id} for doc_id in ids]
        ),
        document_sync_state=AsyncMock(return_value={"restrictions": 0}),
    )
    svc = ExtractionService(writer, None, None, None, None)
    svc.extract_document = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_preview_lists_one_page_without_extraction_or_state_checks():
    svc = service(["b", "c", "d"])
    result = await svc.backfill(
        ExtractionBackfillRequest(limit=2, after_id="a", dry_run=True)
    )
    assert [item.doc_id for item in result.items] == ["b", "c"]
    assert all(item.status == "selected" for item in result.items)
    assert result.has_more and result.next_after_id == "c"
    assert result.extracted == result.failed == 0
    svc.writer.documents_without_restrictions.assert_awaited_once_with(
        after_id="a", limit=3
    )
    svc.writer.document_sync_state.assert_not_awaited()
    svc.extract_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_does_not_block_next_document_or_cursor():
    svc = service(["a", "b", "c"])
    svc.extract_document.side_effect = [
        RuntimeError("LLM unavailable"),
        ExtractResult(doc_id="b", clauses_processed=3, restrictions=2),
    ]
    result = await svc.backfill(ExtractionBackfillRequest(limit=2))
    assert (result.selected, result.extracted, result.failed, result.restrictions) == (
        2,
        1,
        1,
        2,
    )
    assert result.items[0].reason == "LLM unavailable"
    assert result.items[1].clauses_processed == 3
    assert result.next_after_id == "b"
    assert [call.args for call in svc.extract_document.await_args_list] == [
        ("a",),
        ("b",),
    ]
    assert all(not call.kwargs for call in svc.extract_document.await_args_list)


@pytest.mark.asyncio
async def test_recheck_skips_deleted_and_concurrently_extracted_documents():
    svc = service(["a", "b", "c"])
    svc.writer.document_sync_state.side_effect = [
        None,
        {"restrictions": 4},
        {"restrictions": 0},
    ]
    svc.extract_document.return_value = ExtractResult(
        doc_id="c", skipped=True, reason="no clauses in graph"
    )
    result = await svc.backfill(ExtractionBackfillRequest(limit=3))
    assert result.skipped == 3
    assert result.extracted == result.failed == 0
    assert not result.has_more and result.next_after_id is None
    svc.extract_document.assert_awaited_once_with("c")


@pytest.mark.asyncio
async def test_zero_norm_document_does_not_stall_pagination():
    svc = service(["a", "b"])
    svc.extract_document.return_value = ExtractResult(doc_id="a", clauses_processed=2)
    result = await svc.backfill(ExtractionBackfillRequest(limit=1))
    assert result.extracted == 1 and result.restrictions == 0
    assert result.next_after_id == "a" and result.has_more


@pytest.mark.asyncio
async def test_empty_page():
    svc = service([])
    result = await svc.backfill(ExtractionBackfillRequest())
    assert result.selected == 0 and result.items == []
    assert not result.has_more and result.next_after_id is None
    svc.extract_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_propagates():
    svc = service(["a", "b"])
    svc.extract_document.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await svc.backfill(ExtractionBackfillRequest(limit=2))
    assert svc.extract_document.await_count == 1


@pytest.mark.parametrize(
    "payload", [{"limit": 0}, {"limit": 21}, {"after_id": ""}, {"replace": True}]
)
def test_invalid_request(payload):
    with pytest.raises(ValidationError):
        ExtractionBackfillRequest(**payload)


@pytest.mark.asyncio
async def test_endpoint_returns_recovery_results_and_validates_limits(monkeypatch):
    svc = service(["a"])
    monkeypatch.setattr(
        "src.pipeline.router.get_dependencies", lambda: SimpleNamespace(extraction=svc)
    )
    app = FastAPI()
    app.include_router(extraction_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/extraction/backfill", json={"dry_run": True})
        assert response.status_code == 200
        assert response.json()["items"] == [
            {
                "doc_id": "a",
                "status": "selected",
                "clauses_processed": 0,
                "restrictions": 0,
                "reason": None,
            }
        ]
        response = await client.post("/extraction/backfill", json={"limit": 0})
        assert response.status_code == 422
    svc.extract_document.assert_not_awaited()

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from structlog.testing import capture_logs

from src.admin_service.reprocessing import (
    REPLAN_PAGE,
    BulkReprocessing,
    ReprocessingBusy,
)
from src.dto.check_plan import CheckPlanReplanResponse
from src.pipeline.service import ExtractResult


def job(documents, extract=None):
    repository = SimpleNamespace(
        reprocessing_documents=AsyncMock(return_value=documents)
    )
    extraction = SimpleNamespace(
        extract_document=extract or AsyncMock(return_value=ExtractResult(doc_id="d"))
    )
    return BulkReprocessing(repository, extraction)


async def test_all_loaded_documents_processed_sequentially_despite_errors():
    docs = [{"doc_id": str(i), "name": "Document"} for i in range(5)]
    extraction = AsyncMock(
        side_effect=[
            ExtractResult(doc_id="d", restrictions=3),
            RuntimeError("secret-upstream-details"),
            ExtractResult(doc_id="d", incomplete=True, failed_clause_ids=["c"]),
            ExtractResult(doc_id="d", skipped=True),
            ExtractResult(doc_id="d", restrictions=2),
        ]
    )
    service = job(docs, extraction)
    started = await service.start()
    assert started["state"] == "running" and started["total"] == 5
    await service._task
    status = service.status()
    assert status["state"] == "completed_with_errors"
    assert (
        status["processed"],
        status["succeeded"],
        status["failed"],
        status["skipped"],
    ) == (5, 2, 2, 1)
    assert status["restrictions"] == 5
    assert [e["doc_id"] for e in status["errors"]] == ["1", "2", "3"]
    assert "secret-upstream-details" not in str(status)
    assert extraction.await_args_list == [call(str(i), replace=True) for i in range(5)]
    assert started["processed"] == 0  # A detached snapshot, not mutable job state.


async def test_duplicate_start_and_manual_operations_are_blocked():
    entered, release = asyncio.Event(), asyncio.Event()

    async def extract(*args, **kwargs):
        entered.set()
        await release.wait()
        return ExtractResult(doc_id="d")

    service = job([{"doc_id": "d"}], extract)
    await service.start()
    await entered.wait()
    with pytest.raises(ReprocessingBusy):
        await service.start()
    with pytest.raises(ReprocessingBusy):
        async with service.single_operation():
            pass
    assert service.status()["current_document"]["doc_id"] == "d"
    release.set()
    await service._task
    async with service.single_operation():
        with pytest.raises(ReprocessingBusy):
            await service.start()
    await service.start()
    await service._task


@pytest.mark.parametrize("allow_start", [False, True])
async def test_shutdown_cancels_job_and_releases_guard(allow_start):
    entered = asyncio.Event()

    async def extract(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    service = job([{"doc_id": "d"}], extract)
    await service.start()
    if allow_start:
        await entered.wait()
    await service.aclose()
    assert service.status()["state"] == "interrupted"
    async with service.single_operation():
        pass


async def test_empty_snapshot_completes_and_snapshot_failure_allows_retry():
    service = job([])
    service.repository.reprocessing_documents.side_effect = RuntimeError(
        "db unavailable"
    )
    with pytest.raises(RuntimeError):
        await service.start()
    assert service.status()["state"] == "failed"
    service.repository.reprocessing_documents.side_effect = None
    await service.start()
    await service._task
    assert service.status()["state"] == "completed"
    service.extraction.extract_document.assert_not_awaited()


async def test_error_details_are_bounded_but_failure_count_is_complete():
    service = job(
        [{"doc_id": str(i)} for i in range(102)],
        AsyncMock(side_effect=RuntimeError("failed")),
    )
    with capture_logs():
        await service.start()
        await service._task
    status = service.status()
    assert status["failed"] == 102
    assert len(status["errors"]) == 100
    assert status["errors_truncated"]


def replan_page(selected, *, after=None, written=None, failed=0, transitions=None):
    return CheckPlanReplanResponse(
        planner_version=4,
        selected=selected,
        written=selected - failed if written is None else written,
        failed=failed,
        transitions=transitions or {},
        has_more=after is not None,
        next_after_id=after,
        dry_run=False,
    )


def replan_job(pages, total=5):
    replanning = SimpleNamespace(
        count_replannable=AsyncMock(return_value=total),
        replan=AsyncMock(side_effect=pages),
    )
    service = job([])
    service.replanning = replanning
    return service, replanning


async def test_replanning_pages_through_every_plan_of_the_current_version_too():
    service, replanning = replan_job(
        [
            replan_page(
                3,
                after="r3",
                transitions={
                    "auto->auto": 1,
                    "unsupported->auto": 1,
                    "auto->unsupported": 1,
                },
            ),
            replan_page(2, failed=1, transitions={"unsupported->unsupported": 1}),
        ]
    )
    started = await service.start_replanning()
    assert started["kind"] == "replan" and started["total"] == 5
    await service._task

    status = service.status()
    assert status["state"] == "completed_with_errors"
    assert (status["processed"], status["written"], status["failed"]) == (5, 4, 1)
    assert (status["auto"], status["unsupported"]) == (2, 2)
    replanning.count_replannable.assert_awaited_once_with(include_current=True)
    requests = [c.args[0] for c in replanning.replan.await_args_list]
    assert [r.after_id for r in requests] == [None, "r3"]
    assert all(
        r.include_current
        and not r.dry_run
        and not r.include_items
        and r.limit == REPLAN_PAGE
        for r in requests
    )


async def test_replanning_shares_the_lock_and_releases_it_after_a_failed_page():
    entered, release = asyncio.Event(), asyncio.Event()

    async def replan(request):
        entered.set()
        await release.wait()
        raise RuntimeError("graph unavailable")

    service, replanning = replan_job([])
    replanning.replan.side_effect = replan
    await service.start_replanning()
    await entered.wait()
    with pytest.raises(ReprocessingBusy):
        await service.start()
    with pytest.raises(ReprocessingBusy):
        await service.start_replanning()
    release.set()
    with capture_logs():
        await service._task
    assert service.status()["state"] == "failed"
    async with service.single_operation():
        pass


async def test_replanning_count_failure_allows_retry():
    service, replanning = replan_job([replan_page(0)])
    replanning.count_replannable.side_effect = RuntimeError("db unavailable")
    with pytest.raises(RuntimeError):
        await service.start_replanning()
    assert service.status()["state"] == "failed"
    replanning.count_replannable.side_effect = None
    await service.start_replanning()
    await service._task
    assert service.status()["state"] == "completed"

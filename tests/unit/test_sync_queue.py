"""The sync queue runs the most recently changed document first without breaking the order of
one document's own events, and keeps pending jobs across restarts."""

from __future__ import annotations

import asyncio

import pytest

from src.dvd_client.models import DocumentList, DocumentSummary
from src.ingestion.service import IngestResult
from src.sync.queue import SyncJob, SyncQueue
from tests.unit.test_sync_service import (
    FakeDVD,
    FakeExtraction,
    FakeIngestion,
    FakeWriter,
    _svc,
    drain,
)


class RecordingSync:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple] = []
        self._fail_times = fail_times

    async def sync_name(self, name, *, user_id=None, scenario_id=None, replace=False):
        if self._fail_times:
            self._fail_times -= 1
            raise RuntimeError("dvd down")
        self.calls.append(("sync", name, replace))

    async def delete_name(
        self, name, *, user_id=None, scenario_id=None, versions=None, **kwargs
    ):
        self.calls.append(("delete", name, kwargs["document_removed"], versions))

    async def reconcile_document(self, doc_id, *, content_hash=None, version=None):
        self.calls.append(("reconcile", doc_id))


def _name_job(name, changed_at, **actions):
    return SyncJob.for_name(name, changed_at=changed_at, **actions)


@pytest.mark.asyncio
async def test_most_recently_changed_document_runs_first():
    sync, store = RecordingSync(), FakeWriter()
    queue = SyncQueue(sync, store)
    await queue.put(_name_job("old", 1.0, sync=True))
    await queue.put(_name_job("newest", 3.0, sync=True))
    await queue.put(SyncJob.for_document("d-mid", changed_at=2.0))

    await drain(queue)

    assert sync.calls == [
        ("sync", "newest", False),
        ("reconcile", "d-mid"),
        ("sync", "old", False),
    ]
    assert store.jobs == {}


class GatedSync(RecordingSync):
    """Holds each sync until released, so the test can act while a job is running."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def sync_name(self, name, **kwargs):
        await self.release.wait()
        await super().sync_name(name, **kwargs)


async def _wait_running(queue: SyncQueue) -> None:
    while queue.running is None:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_new_upload_overtakes_the_backlog():
    sync = GatedSync()
    queue = SyncQueue(sync, FakeWriter())
    for i in range(3):
        await queue.put(_name_job(f"backlog-{i}", float(i), sync=True))
    await queue.start()
    await _wait_running(queue)
    await queue.put(_name_job("fresh", 100.0, sync=True))
    sync.release.set()
    while queue.pending() or queue.running:
        await asyncio.sleep(0)
    await queue.stop()

    assert [c[1] for c in sync.calls] == [
        "backlog-2",
        "fresh",
        "backlog-1",
        "backlog-0",
    ]


@pytest.mark.asyncio
async def test_events_of_one_document_merge_into_one_job():
    sync = RecordingSync()
    queue = SyncQueue(sync, FakeWriter())
    await queue.put(_name_job("A", 1.0, sync=True))
    await queue.put(_name_job("A", 2.0, delete_versions=["2011"]))
    await queue.put(_name_job("A", 3.0, sync=True, replace=True))

    assert len(queue.pending()) == 1
    await drain(queue)

    # Deletions first, then one sync of what IDU_DVD holds now.
    assert sync.calls == [("delete", "A", False, ["2011"]), ("sync", "A", True)]


@pytest.mark.asyncio
async def test_document_removal_cancels_its_pending_sync():
    sync = RecordingSync()
    queue = SyncQueue(sync, FakeWriter())
    await queue.put(_name_job("A", 1.0, sync=True, replace=True))
    await queue.put(_name_job("A", 2.0, delete_all=True))

    await drain(queue)

    assert sync.calls == [("delete", "A", True, [])]


@pytest.mark.asyncio
async def test_reupload_after_removal_deletes_then_syncs():
    sync = RecordingSync()
    queue = SyncQueue(sync, FakeWriter())
    await queue.put(_name_job("A", 1.0, delete_all=True))
    await queue.put(_name_job("A", 2.0, sync=True))

    await drain(queue)

    assert sync.calls == [("delete", "A", True, []), ("sync", "A", False)]


@pytest.mark.asyncio
async def test_same_scope_name_in_another_user_index_is_a_separate_job():
    queue = SyncQueue(RecordingSync(), FakeWriter())
    await queue.put(_name_job("A", 1.0, sync=True))
    await queue.put(_name_job("A", 1.0, sync=True, user_id="u1", scenario_id="s1"))
    assert len(queue.pending()) == 2


@pytest.mark.asyncio
async def test_pending_jobs_survive_a_restart():
    store = FakeWriter()
    first = SyncQueue(RecordingSync(), store)
    await first.put(_name_job("A", 1.0, sync=True, replace=True))
    await first.put(SyncJob.for_document("d1", changed_at=2.0, content_hash="h"))

    sync = RecordingSync()
    await drain(SyncQueue(sync, store))

    assert sync.calls == [("reconcile", "d1"), ("sync", "A", True)]
    assert store.jobs == {}


@pytest.mark.asyncio
async def test_failed_job_is_retried_and_stays_persisted_until_it_succeeds():
    sync, store = RecordingSync(fail_times=2), FakeWriter()
    queue = SyncQueue(sync, store, retry_delay=0.001)
    await queue.put(_name_job("A", 1.0, sync=True))
    await queue.start()
    while not sync.calls:
        assert "name:::A" in store.jobs
        await asyncio.sleep(0.001)
    while queue.running:
        await asyncio.sleep(0)
    await queue.stop()

    assert sync.calls == [("sync", "A", False)]
    assert store.jobs == {}


@pytest.mark.asyncio
async def test_event_arriving_while_its_document_runs_is_kept_for_another_pass():
    sync, store = GatedSync(), FakeWriter()
    queue = SyncQueue(sync, store)
    await queue.put(_name_job("A", 1.0, sync=True))
    await queue.start()
    await _wait_running(queue)
    await queue.put(_name_job("A", 2.0, sync=True, replace=True))
    sync.release.set()
    while len(sync.calls) < 2:
        assert (
            "name:::A" in store.jobs
        )  # the newer event is never dropped from the store
        await asyncio.sleep(0)
    while queue.running:
        await asyncio.sleep(0)
    await queue.stop()

    assert sync.calls == [("sync", "A", False), ("sync", "A", True)]
    assert store.jobs == {}


def _listing(*docs):
    return DocumentList(count=len(docs), documents=list(docs))


@pytest.mark.asyncio
async def test_reconcile_visits_the_newest_documents_first():
    listing = _listing(
        DocumentSummary(doc_id="a", name="A", uploaded_at="2026-09-25T10:00:00+00:00"),
        DocumentSummary(doc_id="undated", name="B"),
        DocumentSummary(doc_id="c", name="C", uploaded_at="2026-09-27T10:00:00+00:00"),
    )
    ext = FakeExtraction()
    await _svc(extraction=ext, dvd=FakeDVD(listing=listing)).reconcile()
    assert [doc_id for doc_id, _ in ext.calls] == ["c", "a", "undated"]


@pytest.mark.asyncio
async def test_reconcile_with_a_queue_only_schedules_changed_documents():
    listing = _listing(
        DocumentSummary(
            doc_id="new", name="A", content_hash="h1", uploaded_at="2026-09-27"
        ),
        DocumentSummary(doc_id="same", name="C", content_hash="h3"),
    )
    writer = FakeWriter(stored=[{"doc_id": "same", "content_hash": "h3"}])
    ext = FakeExtraction()
    svc = _svc(extraction=ext, writer=writer, dvd=FakeDVD(listing=listing))
    svc.queue = SyncQueue(svc, writer)

    result = await svc.reconcile()

    assert (result.queued, result.added, result.unchanged) == (1, 1, 1)
    assert ext.calls == []  # nothing ran inline
    assert list(writer.jobs) == ["doc:new"]
    await drain(svc.queue)
    assert ext.calls == [("new", False)]


@pytest.mark.asyncio
async def test_queued_reconcile_skips_a_document_an_event_already_synced():
    stored = {"doc_id": "d1", "content_hash": "h2", "version": ""}
    writer = FakeWriter(stored=[stored])
    ing = FakeIngestion(IngestResult(doc_id="d1", content_hash="h2"))
    ext = FakeExtraction()
    svc = _svc(ingestion=ing, extraction=ext, writer=writer)

    # Queued while the graph still held the old hash; the Kafka event got there first.
    outcome = await svc.reconcile_document("d1", content_hash="h2", version="")

    assert outcome == "unchanged"
    assert ing.calls == [] and ext.calls == []

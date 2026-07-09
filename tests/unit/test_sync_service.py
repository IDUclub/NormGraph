"""SyncService ties ingest + extract, handles deletes and the reconcile diff — all faked."""

from __future__ import annotations

import pytest

from src.dvd_client.models import DocumentList, DocumentSummary
from src.ingestion.service import IngestResult
from src.pipeline.service import ExtractResult
from src.sync.service import SyncService


class FakeIngestion:
    def __init__(self, result: IngestResult | None = None) -> None:
        self.result = result or IngestResult(doc_id="d1", clauses=3, pruned_clauses=1)
        self.calls: list[tuple[str, bool]] = []

    async def ingest_document(self, doc_id, *, replace=False):
        self.calls.append((doc_id, replace))
        return IngestResult(
            doc_id=doc_id,
            clauses=self.result.clauses,
            pruned_clauses=self.result.pruned_clauses if replace else 0,
            skipped=self.result.skipped,
            reason=self.result.reason,
        )


class FakeExtraction:
    def __init__(self, restrictions: int = 5) -> None:
        self.restrictions = restrictions
        self.calls: list[tuple[str, bool]] = []

    async def extract_document(self, doc_id, *, replace=False):
        self.calls.append((doc_id, replace))
        return ExtractResult(
            doc_id=doc_id, restrictions=self.restrictions, replaced=replace
        )


class FakeWriter:
    def __init__(self, stored=None, by_name=None) -> None:
        self._stored = stored or []
        self._by_name = by_name or {}
        self.deleted: list[str] = []

    async def stored_documents(self):
        return list(self._stored)

    async def documents_by_name(self, name):
        return list(self._by_name.get(name, []))

    async def delete_document(self, doc_id):
        self.deleted.append(doc_id)
        return {"clauses": 2, "restrictions": 4}


class FakeDVD:
    def __init__(self, *, ids_by_name=None, listing=None, raises=False) -> None:
        self._ids = ids_by_name or {}
        self._listing = listing
        self._raises = raises

    async def resolve_doc_ids(self, name):
        return list(self._ids.get(name, []))

    async def list_library_documents(self):
        if self._raises:
            raise RuntimeError("dvd down")
        return self._listing


def _svc(ingestion=None, extraction=None, writer=None, dvd=None) -> SyncService:
    return SyncService(
        dvd or FakeDVD(),
        writer or FakeWriter(),
        ingestion or FakeIngestion(),
        extraction or FakeExtraction(),
    )


@pytest.mark.asyncio
async def test_sync_document_chains_ingest_then_extract():
    ing, ext = FakeIngestion(), FakeExtraction(restrictions=7)
    svc = _svc(ingestion=ing, extraction=ext)

    result = await svc.sync_document("d1", replace=True)

    assert ing.calls == [("d1", True)]
    assert ext.calls == [("d1", True)]
    assert result.restrictions == 7
    assert result.replaced is True
    assert result.pruned_clauses == 1


@pytest.mark.asyncio
async def test_sync_document_skips_extract_when_ingest_skipped():
    ing = FakeIngestion(
        IngestResult(doc_id="d1", skipped=True, reason="not found in DVD")
    )
    ext = FakeExtraction()
    svc = _svc(ingestion=ing, extraction=ext)

    result = await svc.sync_document("d1")

    assert result.skipped is True
    assert ext.calls == []  # extraction never runs for a missing document


@pytest.mark.asyncio
async def test_sync_name_resolves_and_tags_each_result():
    dvd = FakeDVD(ids_by_name={"СП 42": ["d1", "d2"]})
    ext = FakeExtraction()
    svc = _svc(dvd=dvd, extraction=ext)

    results = await svc.sync_name("СП 42")

    assert [r.doc_id for r in results] == ["d1", "d2"]
    assert all(r.name == "СП 42" for r in results)


@pytest.mark.asyncio
async def test_sync_name_unknown_is_skipped():
    svc = _svc(dvd=FakeDVD(ids_by_name={}))
    results = await svc.sync_name("nope")
    assert len(results) == 1 and results[0].skipped is True


@pytest.mark.asyncio
async def test_delete_name_removes_all_versions_when_document_removed():
    writer = FakeWriter(
        by_name={
            "СП 42": [
                {"doc_id": "d1", "version": "2016"},
                {"doc_id": "d2", "version": "2011"},
            ]
        }
    )
    svc = _svc(writer=writer)

    result = await svc.delete_name("СП 42", versions=["2016"], document_removed=True)

    assert set(writer.deleted) == {"d1", "d2"}
    assert result.documents_deleted == 2
    assert result.restrictions_deleted == 8


@pytest.mark.asyncio
async def test_delete_name_version_scoped_when_document_survives():
    writer = FakeWriter(
        by_name={
            "СП 42": [
                {"doc_id": "d1", "version": "2016"},
                {"doc_id": "d2", "version": "2011"},
            ]
        }
    )
    svc = _svc(writer=writer)

    result = await svc.delete_name("СП 42", versions=["2011"], document_removed=False)

    assert writer.deleted == ["d2"]
    assert result.documents_deleted == 1


@pytest.mark.asyncio
async def test_delete_name_version_scoped_without_versions_deletes_nothing():
    writer = FakeWriter(by_name={"СП 42": [{"doc_id": "d1", "version": "2016"}]})
    svc = _svc(writer=writer)

    result = await svc.delete_name("СП 42", versions=[], document_removed=False)

    assert (
        writer.deleted == []
    )  # a version-scoped deletion naming no versions is a no-op
    assert result.documents_deleted == 0


@pytest.mark.asyncio
async def test_reconcile_adds_updates_and_deletes():
    listing = DocumentList(
        count=2,
        documents=[
            DocumentSummary(doc_id="new", name="A", content_hash="h1"),
            DocumentSummary(doc_id="chg", name="B", content_hash="h2-new"),
            DocumentSummary(doc_id="same", name="C", content_hash="h3"),
        ],
    )
    writer = FakeWriter(
        stored=[
            {"doc_id": "chg", "content_hash": "h2-old"},
            {"doc_id": "same", "content_hash": "h3"},
            {"doc_id": "gone", "content_hash": "h4"},
        ]
    )
    ing, ext = FakeIngestion(), FakeExtraction()
    svc = _svc(
        ingestion=ing, extraction=ext, writer=writer, dvd=FakeDVD(listing=listing)
    )

    result = await svc.reconcile()

    assert result.added == 1  # "new"
    assert result.updated == 1  # "chg" (hash changed → replace)
    assert result.unchanged == 1  # "same"
    assert result.deleted == 1  # "gone"
    assert writer.deleted == ["gone"]
    # The changed document is re-synced with replace=True; the new one without.
    assert ("chg", True) in ext.calls
    assert ("new", False) in ext.calls


@pytest.mark.asyncio
async def test_reconcile_skips_change_without_hash():
    listing = DocumentList(
        count=1,
        documents=[DocumentSummary(doc_id="d1", name="A", content_hash=None)],
    )
    writer = FakeWriter(stored=[{"doc_id": "d1", "content_hash": "h"}])
    ext = FakeExtraction()
    svc = _svc(extraction=ext, writer=writer, dvd=FakeDVD(listing=listing))

    result = await svc.reconcile()

    assert result.unchanged == 1 and result.updated == 0
    assert ext.calls == []  # no hash on the DVD side → treated as unchanged


@pytest.mark.asyncio
async def test_reconcile_skipped_when_dvd_unreachable():
    svc = _svc(dvd=FakeDVD(raises=True))
    result = await svc.reconcile()
    assert result.skipped is True and result.reason == "dvd unreachable"

"""SyncService ties ingest + extract, handles deletes and the reconcile diff — all faked."""

from __future__ import annotations

import pytest

from src.dvd_client.models import DocumentList, DocumentSummary
from src.ingestion.service import IngestResult
from src.pipeline.service import ExtractResult
from src.sync.consumer import DocumentDeletedHandler
from src.sync.events import DocumentDeleted
from src.sync.service import SyncService


class FakeIngestion:
    def __init__(self, result: IngestResult | None = None) -> None:
        self.result = result or IngestResult(doc_id="d1", clauses=3, pruned_clauses=1)
        self.calls: list[tuple[str, str | None, str | None, bool]] = []

    async def ingest_document(
        self, doc_id, *, user_id=None, scenario_id=None, replace=False
    ):
        self.calls.append((doc_id, user_id, scenario_id, replace))
        return IngestResult(
            doc_id=doc_id,
            clauses=self.result.clauses,
            pruned_clauses=self.result.pruned_clauses if replace else 0,
            content_hash=self.result.content_hash,
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
    def __init__(
        self, stored=None, by_name=None, sync_state=None, scope_delete_result=None
    ) -> None:
        self._stored = stored or []
        self._by_name = by_name or {}
        self._sync_state = sync_state or {}
        self._scope_delete_result = scope_delete_result or {
            "documents": 2,
            "clauses": 5,
            "restrictions": 9,
        }
        self.deleted: list[str] = []
        self.documents_by_name_calls: list[tuple[str, str | None, str | None]] = []
        self.scope_delete_calls: list[tuple[str, str]] = []

    async def document_sync_state(self, doc_id):
        return self._sync_state.get(doc_id)

    async def stored_documents(self):
        return list(self._stored)

    async def documents_by_name(self, name, *, user_id=None, scenario_id=None):
        self.documents_by_name_calls.append((name, user_id, scenario_id))
        return list(self._by_name.get(name, []))

    async def delete_document(self, doc_id):
        self.deleted.append(doc_id)
        return {"clauses": 2, "restrictions": 4}

    async def delete_scope(self, user_id, scenario_id):
        self.scope_delete_calls.append((user_id, scenario_id))
        return dict(self._scope_delete_result)


class FakeDVD:
    def __init__(
        self,
        *,
        ids_by_name=None,
        user_ids_by_scope=None,
        listing=None,
        raises=False,
    ) -> None:
        self._ids = ids_by_name or {}
        self._user_ids = user_ids_by_scope or {}
        self._listing = listing
        self._raises = raises

    async def resolve_doc_ids(self, name):
        return list(self._ids.get(name, []))

    async def resolve_user_doc_ids(self, user_id, scenario_id, name):
        return list(self._user_ids.get((user_id, scenario_id, name), []))

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

    assert ing.calls == [("d1", None, None, True)]
    assert ext.calls == [("d1", True)]
    assert result.restrictions == 7
    assert result.replaced is True
    assert result.pruned_clauses == 1


@pytest.mark.asyncio
async def test_guard_skips_extraction_when_unchanged():
    # Already synced (same content_hash, has restrictions) → cheap ingest runs, extraction skipped.
    ing = FakeIngestion(IngestResult(doc_id="d1", clauses=3, content_hash="h1"))
    ext = FakeExtraction()
    writer = FakeWriter(sync_state={"d1": {"content_hash": "h1", "restrictions": 5}})
    svc = _svc(ingestion=ing, extraction=ext, writer=writer)

    result = await svc.sync_document("d1")  # replace=False

    assert result.extraction_skipped is True
    assert result.restrictions == 5  # prior count preserved
    assert ext.calls == []  # the expensive step is skipped
    assert ing.calls == [("d1", None, None, False)]  # ingest still ran (idempotent)


@pytest.mark.asyncio
async def test_guard_extracts_when_content_changed():
    ing = FakeIngestion(IngestResult(doc_id="d1", clauses=3, content_hash="h2"))
    ext = FakeExtraction(restrictions=9)
    writer = FakeWriter(sync_state={"d1": {"content_hash": "h1", "restrictions": 5}})
    svc = _svc(ingestion=ing, extraction=ext, writer=writer)

    result = await svc.sync_document("d1")

    assert result.extraction_skipped is False
    assert ext.calls == [("d1", False)]
    assert result.restrictions == 9


@pytest.mark.asyncio
async def test_guard_extracts_when_no_prior_restrictions():
    # Structure was ingested before but never extracted → extraction must run.
    ing = FakeIngestion(IngestResult(doc_id="d1", clauses=3, content_hash="h1"))
    ext = FakeExtraction(restrictions=4)
    writer = FakeWriter(sync_state={"d1": {"content_hash": "h1", "restrictions": 0}})
    svc = _svc(ingestion=ing, extraction=ext, writer=writer)

    result = await svc.sync_document("d1")

    assert result.extraction_skipped is False
    assert ext.calls == [("d1", False)]


@pytest.mark.asyncio
async def test_guard_ignored_on_replace():
    # replace=True always re-extracts, regardless of unchanged content.
    ing = FakeIngestion(IngestResult(doc_id="d1", clauses=3, content_hash="h1"))
    ext = FakeExtraction()
    writer = FakeWriter(sync_state={"d1": {"content_hash": "h1", "restrictions": 5}})
    svc = _svc(ingestion=ing, extraction=ext, writer=writer)

    result = await svc.sync_document("d1", replace=True)

    assert result.extraction_skipped is False
    assert ext.calls == [("d1", True)]


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
async def test_sync_name_uses_scoped_resolution_when_scope_given():
    # Scope-aware resolution goes through DVDClient.resolve_user_doc_ids, not resolve_doc_ids,
    # since /library/lookup is scope-blind and could resolve to someone else's same-named doc.
    dvd = FakeDVD(user_ids_by_scope={("u1", "s1", "doc"): ["ud1"]})
    ing = FakeIngestion()
    svc = _svc(dvd=dvd, ingestion=ing)

    results = await svc.sync_name("doc", user_id="u1", scenario_id="s1")

    assert [r.doc_id for r in results] == ["ud1"]
    assert ing.calls == [("ud1", "u1", "s1", False)]


@pytest.mark.asyncio
async def test_delete_name_forwards_scope_to_writer():
    writer = FakeWriter(by_name={"doc": [{"doc_id": "ud1", "version": "1"}]})
    svc = _svc(writer=writer)

    await svc.delete_name("doc", user_id="u1", scenario_id="s1")

    assert writer.documents_by_name_calls == [("doc", "u1", "s1")]


@pytest.mark.asyncio
async def test_delete_scope_wipes_and_returns_counts():
    writer = FakeWriter(
        scope_delete_result={"documents": 2, "clauses": 5, "restrictions": 9}
    )
    svc = _svc(writer=writer)

    result = await svc.delete_scope("u1", "s1")

    assert writer.scope_delete_calls == [("u1", "s1")]
    assert result.documents_deleted == 2
    assert result.clauses_deleted == 5
    assert result.restrictions_deleted == 9


@pytest.mark.asyncio
async def test_index_wipe_burst_deletes_each_document_independently():
    # Simulates IDU_DVD's fixed UserIndexService.delete_index: one DocumentDeleted event per
    # document name in the wiped (user_id, scenario_id) index, delivered in sequence — each must
    # resolve and delete only its own doc_ids, with no cross-contamination between documents.
    writer = FakeWriter(
        by_name={
            "Doc A": [{"doc_id": "da1", "version": "v1"}],
            "Doc B": [
                {"doc_id": "db1", "version": "v1"},
                {"doc_id": "db2", "version": "v2"},
            ],
        }
    )
    svc = _svc(writer=writer)
    handler = DocumentDeletedHandler(svc)

    await handler.handle(
        DocumentDeleted(
            document_name="Doc A",
            versions_removed=["v1"],
            document_removed=True,
            user_id="u1",
            scenario_id="s1",
        ),
        None,
    )
    await handler.handle(
        DocumentDeleted(
            document_name="Doc B",
            versions_removed=["v1", "v2"],
            document_removed=True,
            user_id="u1",
            scenario_id="s1",
        ),
        None,
    )

    assert writer.documents_by_name_calls == [
        ("Doc A", "u1", "s1"),
        ("Doc B", "u1", "s1"),
    ]
    assert writer.deleted == [
        "da1",
        "db1",
        "db2",
    ]  # each name's own doc_ids, nothing extra


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

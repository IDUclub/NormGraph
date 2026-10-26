"""SyncService ties ingest + extract, handles deletes and the reconcile diff — all faked."""

from __future__ import annotations

import asyncio

import pytest

from src.dvd_client.models import DocumentList, DocumentSummary
from src.ingestion.service import IngestResult
from src.pipeline.service import ExtractResult
from src.sync.consumer import DocumentDeletedHandler
from src.sync.events import DocumentDeleted
from src.sync.queue import SyncQueue
from src.sync.service import SyncService


async def drain(queue: SyncQueue) -> None:
    """Run the queue until every job it holds has finished."""
    await queue.start()
    while queue.pending() or queue.running is not None:
        await asyncio.sleep(0)
    await queue.stop()


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
            carried_clauses=self.result.carried_clauses if replace else 0,
            content_hash=self.result.content_hash,
            skipped=self.result.skipped,
            reason=self.result.reason,
        )


class FakeExtraction:
    def __init__(self, restrictions: int = 5) -> None:
        self.restrictions = restrictions
        self.calls: list[tuple[str, bool]] = []

    async def extract_document(
        self, doc_id, *, replace=False, clause_ids=None, reuse=False
    ):
        self.reuse = reuse
        self.calls.append(
            (doc_id, replace) if clause_ids is None else (doc_id, clause_ids)
        )
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
        self.jobs: dict[str, dict] = {}
        self.documents_by_name_calls: list[tuple[str, str | None, str | None]] = []
        self.scope_delete_calls: list[tuple[str, str]] = []

    async def document_sync_state(self, doc_id):
        return self._sync_state.get(doc_id)

    async def stored_documents(self):
        return list(self._stored)

    async def stored_document(self, doc_id):
        return next((row for row in self._stored if row["doc_id"] == doc_id), None)

    async def save_sync_job(self, props):
        self.jobs[props["key"]] = props

    async def delete_sync_job(self, key):
        self.jobs.pop(key, None)

    async def sync_jobs(self):
        return list(self.jobs.values())

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
    queue = SyncQueue(_svc(writer=writer), writer)
    handler = DocumentDeletedHandler(queue)

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

    await drain(queue)

    assert sorted(writer.documents_by_name_calls) == [
        ("Doc A", "u1", "s1"),
        ("Doc B", "u1", "s1"),
    ]
    # each name's own doc_ids, nothing extra
    assert sorted(writer.deleted) == ["da1", "db1", "db2"]
    assert writer.jobs == {}


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
async def test_reconcile_refreshes_a_relabelled_edition_without_re_extraction():
    listing = DocumentList(
        count=1,
        documents=[
            DocumentSummary(
                doc_id="d1",
                name="СП 2.4.3648-20",
                version="СП 2.4.3648-20",
                content_hash="h",
            )
        ],
    )
    writer = FakeWriter(
        stored=[{"doc_id": "d1", "content_hash": "h", "version": "3648"}]
    )
    ing, ext = FakeIngestion(), FakeExtraction()
    svc = _svc(
        ingestion=ing, extraction=ext, writer=writer, dvd=FakeDVD(listing=listing)
    )

    result = await svc.reconcile()

    assert result.relabelled == 1 and result.unchanged == 0
    assert ing.calls == [("d1", None, None, False)]
    assert ext.calls == []


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


async def test_incomplete_extraction_bypasses_unchanged_guard_and_surfaces_warnings():
    ing = FakeIngestion(IngestResult(doc_id="d1", clauses=3, content_hash="h1"))
    writer = FakeWriter(
        sync_state={
            "d1": {
                "content_hash": "h1",
                "restrictions": 5,
                "extraction_incomplete": True,
            }
        }
    )

    class PartialExtraction(FakeExtraction):
        async def extract_document(self, doc_id, *, replace=False, reuse=False):
            self.calls.append((doc_id, replace))
            return ExtractResult(
                doc_id=doc_id,
                restrictions=2,
                incomplete=True,
                failed_clause_ids=["c1"],
                warnings=["c1: invalid_llm_output"],
            )

    ext = PartialExtraction()
    result = await _svc(ingestion=ing, extraction=ext, writer=writer).sync_document(
        "d1"
    )
    assert ext.calls == [("d1", False)]
    assert not result.extraction_skipped and result.extraction_incomplete
    assert result.failed_clause_ids == ["c1"]
    assert result.warnings == ["c1: invalid_llm_output"]


def _incomplete(failed, *, pruned=0):
    listing = DocumentList(
        count=1, documents=[DocumentSummary(doc_id="d1", name="A", content_hash="h1")]
    )
    state = {
        "doc_id": "d1",
        "content_hash": "h1",
        "restrictions": 5,
        "extraction_incomplete": True,
        "extraction_failed_clause_ids": failed,
    }
    ing = FakeIngestion(
        IngestResult(doc_id="d1", clauses=3, content_hash="h1", pruned_clauses=pruned)
    )
    ext = FakeExtraction()
    svc = _svc(
        writer=FakeWriter(stored=[state]),
        ingestion=ing,
        extraction=ext,
        dvd=FakeDVD(listing=listing),
    )
    return svc, ing, ext


async def test_reconcile_retries_only_the_failed_clauses_of_an_incomplete_document():
    svc, ing, ext = _incomplete(["c1", "c7"])
    result = await svc.reconcile()
    # Stale clauses are pruned first; nothing was, so only the two failures are redone.
    assert ing.calls == [("d1", None, None, True)]
    assert ext.calls == [("d1", ["c1", "c7"])]
    assert result.updated == 1 and result.unchanged == 0


async def test_reconcile_re_extracts_a_document_reparsed_under_the_same_hash():
    svc, _, ext = _incomplete(["c1"], pruned=897)
    await svc.reconcile()
    assert ext.calls == [("d1", True)]


async def test_reconcile_re_extracts_when_the_interrupted_run_left_no_failed_list():
    svc, _, ext = _incomplete(None)
    await svc.reconcile()
    assert ext.calls == [("d1", True)]


@pytest.mark.asyncio
async def test_a_changed_document_reuses_unchanged_clauses():
    ing = FakeIngestion(
        IngestResult(doc_id="d1", clauses=3, pruned_clauses=2, carried_clauses=2)
    )
    ext = FakeExtraction()
    svc = _svc(ingestion=ing, extraction=ext)

    result = await svc.sync_document("d1", replace=True)

    assert ext.calls == [("d1", True)] and ext.reuse is True
    assert result.carried_clauses == 2
    # A first sync has nothing to reuse.
    await svc.sync_document("d2")
    assert ext.reuse is False


class FakeLinker:
    def __init__(self, changed: dict[str, list[str]]) -> None:
        self.changed = changed
        self.calls: list[str] = []

    async def link(self, doc_id):
        self.calls.append(doc_id)
        return {target: list(ids) for target, ids in self.changed.items()}


class ExplainingWriter(FakeWriter):
    def __init__(self, *args, explained=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.explained = explained or {}
        self.forgotten: list[str] = []

    async def forget_extracted(self, node_ids):
        self.forgotten.extend(node_ids)

    async def explained_clauses(self, doc_id):
        return self.explained.get(doc_id, {})


@pytest.mark.asyncio
async def test_an_explanation_re_extracts_the_clauses_it_explains():
    ext, writer = FakeExtraction(), ExplainingWriter()
    linker = FakeLinker({"rules": ["r4", "r7"]})
    svc = SyncService(FakeDVD(), writer, FakeIngestion(), ext, linker)

    result = await svc.sync_document("letter", replace=True)

    assert linker.calls == ["letter"]
    # the letter's own extraction, then only the explained clauses of the rules
    assert ext.calls == [("letter", True), ("rules", ["r4", "r7"])]
    assert writer.forgotten == ["r4", "r7"]
    assert result.explained_clauses == 2


@pytest.mark.asyncio
async def test_an_unchanged_document_re_extracts_only_its_newly_explained_clauses():
    ing = FakeIngestion(IngestResult(doc_id="rules", clauses=3, content_hash="h1"))
    ext = FakeExtraction()
    writer = ExplainingWriter(
        sync_state={"rules": {"content_hash": "h1", "restrictions": 5}}
    )
    svc = SyncService(FakeDVD(), writer, ing, ext, FakeLinker({"rules": ["r4"]}))

    result = await svc.sync_document("rules")

    assert ext.calls == [("rules", ["r4"])]
    assert writer.forgotten == ["r4"]
    assert result.extraction_skipped is False
    assert result.explained_clauses == 1


@pytest.mark.asyncio
async def test_user_documents_are_not_linked_to_explanations():
    linker = FakeLinker({"rules": ["r4"]})
    svc = SyncService(
        FakeDVD(), ExplainingWriter(), FakeIngestion(), FakeExtraction(), linker
    )

    await svc.sync_document("mine", user_id="u", scenario_id="s")

    assert linker.calls == []


@pytest.mark.asyncio
async def test_deleting_an_explanation_re_extracts_what_it_explained():
    ext = FakeExtraction()
    writer = ExplainingWriter(
        by_name={"Письмо": [{"doc_id": "letter", "version": "2024"}]},
        explained={"letter": {"rules": ["r4"]}},
    )
    svc = SyncService(FakeDVD(), writer, FakeIngestion(), ext, FakeLinker({}))

    await svc.delete_name("Письмо")

    assert writer.deleted == ["letter"]
    assert ext.calls == [("rules", ["r4"])]


class ScopeWriter(FakeWriter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.upserts: list[dict] = []

    async def upsert_document(self, props):
        self.upserts.append(props)


@pytest.mark.asyncio
async def test_reconcile_copies_a_territory_tagged_after_the_sync():
    listing = DocumentList(
        documents=[
            DocumentSummary(
                doc_id="pzz",
                name="ПЗЗ",
                content_hash="h",
                version="2019",
                territory_id=73,
                territory_name="Гатчинское городское поселение",
                document_level="municipal",
            ),
            DocumentSummary(doc_id="sp", name="СП", content_hash="h2", version="2016"),
        ]
    )
    writer = ScopeWriter(
        stored=[
            {"doc_id": "pzz", "content_hash": "h", "version": "2019"},
            {"doc_id": "sp", "content_hash": "h2", "version": "2016"},
        ]
    )
    svc = _svc(writer=writer, dvd=FakeDVD(listing=listing))

    result = await svc.reconcile()

    assert result.unchanged == 2
    assert writer.upserts == [
        {
            "doc_id": "pzz",
            "territory_id": 73,
            "territory_name": "Гатчинское городское поселение",
            "document_level": "municipal",
        }
    ]

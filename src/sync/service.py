"""Incremental sync between IDU_DVD and the graph.

Ties the structural ingestion and the restriction extraction into one document lifecycle and adds
the operations the event-driven and startup paths need on top of them:

* ``sync_document`` / ``sync_name`` — ingest + extract a document (``replace=True`` re-does a
  changed document incrementally: prune dropped clauses, re-derive its restrictions);
* ``delete_name`` — drop a document (or specific versions) removed from IDU_DVD;
* ``reconcile`` — a startup catch-up pass that diffs the DVD library listing against the graph
  (by ``content_hash``) to pick up documents added, changed or deleted while the consumer was down.

The Kafka consumer (``src/sync/consumer.py``) and the ``/sync`` router both drive this service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import structlog

from src.dvd_client import DVDClient
from src.graph.writer import GraphWriter
from src.ingestion.service import IngestionService
from src.pipeline.service import ExtractionService

log = structlog.get_logger(__name__)


@dataclass
class SyncResult:
    doc_id: str = ""
    name: str | None = None
    clauses: int = 0
    restrictions: int = 0
    pruned_clauses: int = 0
    replaced: bool = False
    extraction_skipped: bool = False
    skipped: bool = False
    reason: str | None = None


@dataclass
class DeleteResult:
    name: str
    documents_deleted: int = 0
    clauses_deleted: int = 0
    restrictions_deleted: int = 0
    doc_ids: list[str] = field(default_factory=list)


@dataclass
class ReconcileResult:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped: bool = False
    reason: str | None = None


class SyncService:
    def __init__(
        self,
        dvd: DVDClient,
        writer: GraphWriter,
        ingestion: IngestionService,
        extraction: ExtractionService,
    ) -> None:
        self.dvd = dvd
        self.writer = writer
        self.ingestion = ingestion
        self.extraction = extraction

    async def sync_document(self, doc_id: str, *, replace: bool = False) -> SyncResult:
        """Ingest a document and extract its restrictions (both idempotent).

        Idempotency guard: on a non-``replace`` sync, if the document is already in the graph
        with the same ``content_hash`` and already has restrictions, the (cheap) ingest still
        runs but the expensive extraction is skipped. This makes a replay of an already-synced
        document — first-boot ``earliest`` backlog, a redelivered event, a retry, or overlap with
        reconcile — a near-no-op instead of a full LLM re-extraction.
        """
        prev = None if replace else await self.writer.document_sync_state(doc_id)

        ing = await self.ingestion.ingest_document(doc_id, replace=replace)
        if ing.skipped:
            return SyncResult(doc_id=doc_id, skipped=True, reason=ing.reason)

        unchanged = bool(
            prev
            and prev.get("restrictions", 0) > 0
            and prev.get("content_hash")
            and prev["content_hash"] == ing.content_hash
        )
        if unchanged:
            result = SyncResult(
                doc_id=doc_id,
                clauses=ing.clauses,
                restrictions=prev["restrictions"],
                pruned_clauses=ing.pruned_clauses,
                replaced=replace,
                extraction_skipped=True,
            )
            log.info("document_sync_skipped_extraction", **asdict(result))
            return result

        ext = await self.extraction.extract_document(doc_id, replace=replace)
        result = SyncResult(
            doc_id=doc_id,
            clauses=ing.clauses,
            restrictions=ext.restrictions,
            pruned_clauses=ing.pruned_clauses,
            replaced=replace,
        )
        log.info("document_synced", **asdict(result))
        return result

    async def sync_name(self, name: str, *, replace: bool = False) -> list[SyncResult]:
        """Sync every corpus/version entry registered under a document name."""
        doc_ids = await self.dvd.resolve_doc_ids(name)
        if not doc_ids:
            return [SyncResult(name=name, skipped=True, reason=f"unknown name: {name}")]
        results = []
        for doc_id in doc_ids:
            result = await self.sync_document(doc_id, replace=replace)
            result.name = name
            results.append(result)
        return results

    async def delete_name(
        self,
        name: str,
        *,
        versions: list[str] | None = None,
        document_removed: bool = True,
    ) -> DeleteResult:
        """Delete a document removed from IDU_DVD.

        When ``document_removed`` is false, only the graph documents whose version matches
        ``versions`` are dropped; otherwise every document under the name is removed.
        """
        stored = await self.writer.documents_by_name(name)
        if document_removed:
            targets = [d["doc_id"] for d in stored]
        elif versions:
            wanted = set(versions)
            targets = [
                d["doc_id"]
                for d in stored
                if d.get("version") in wanted or d.get("version_id") in wanted
            ]
        else:
            # A version-scoped deletion that names no versions removes nothing.
            targets = []

        result = DeleteResult(name=name)
        for doc_id in targets:
            counts = await self.writer.delete_document(doc_id)
            result.documents_deleted += 1
            result.clauses_deleted += counts.get("clauses", 0)
            result.restrictions_deleted += counts.get("restrictions", 0)
            result.doc_ids.append(doc_id)
        log.info("documents_deleted", **asdict(result))
        return result

    async def reconcile(self) -> ReconcileResult:
        """Catch-up pass: align the graph with the current IDU_DVD library listing.

        A document present in DVD but not in the graph is synced; one whose ``content_hash``
        changed is re-synced with ``replace=True``; one in the graph but no longer in DVD is
        deleted. Documents without a ``content_hash`` on the DVD side are treated as unchanged
        (event-driven updates keep them current) to avoid reprocessing on every startup.
        """
        try:
            listing = await self.dvd.list_library_documents()
        except Exception as exc:  # noqa: BLE001 — reconcile must not break startup
            log.warning("reconcile_dvd_unreachable", error=str(exc))
            return ReconcileResult(skipped=True, reason="dvd unreachable")

        stored = {row["doc_id"]: row for row in await self.writer.stored_documents()}
        result = ReconcileResult()
        seen: set[str] = set()

        for summary in listing.documents:
            if not summary.doc_id:
                continue
            seen.add(summary.doc_id)
            prev = stored.get(summary.doc_id)
            try:
                if prev is None:
                    await self.sync_document(summary.doc_id, replace=False)
                    result.added += 1
                elif (
                    summary.content_hash
                    and prev.get("content_hash") != summary.content_hash
                ):
                    await self.sync_document(summary.doc_id, replace=True)
                    result.updated += 1
                else:
                    result.unchanged += 1
            # A single failing document must not abort the whole reconcile pass.
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "reconcile_sync_failed", doc_id=summary.doc_id, error=str(exc)
                )
                result.failed += 1

        for doc_id in set(stored) - seen:
            try:
                await self.writer.delete_document(doc_id)
                result.deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.error("reconcile_delete_failed", doc_id=doc_id, error=str(exc))
                result.failed += 1

        log.info("reconcile_done", **asdict(result))
        return result

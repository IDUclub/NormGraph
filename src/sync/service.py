"""Incremental sync between IDU_DVD and the graph.

Ties the structural ingestion and the restriction extraction into one document lifecycle and adds
the operations the event-driven and startup paths need on top of them:

* ``sync_document`` / ``sync_name`` — ingest + extract a document (``replace=True`` re-does a
  changed document incrementally: prune dropped clauses, re-derive its restrictions);
* ``delete_name`` — drop a document (or specific versions) removed from IDU_DVD;
* ``reconcile`` — a startup catch-up pass that diffs the DVD library listing against the graph
  (by ``content_hash``) to pick up documents added, changed or deleted while the consumer was down.

The Kafka consumer (``src/sync/consumer.py``) and reconcile schedule their work on the newest-first
``SyncQueue`` (``src/sync/queue.py``); the ``/sync`` router drives single documents directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import structlog

from src.dvd_client import DVDClient
from src.graph.writer import GraphWriter
from src.ingestion.service import IngestionService
from src.pipeline.service import ExtractionService
from src.sync.queue import SyncJob, SyncQueue, changed_at_from_iso

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
    extraction_incomplete: bool = False
    warnings: list[str] = field(default_factory=list)
    failed_clause_ids: list[str] = field(default_factory=list)


@dataclass
class DeleteResult:
    name: str
    documents_deleted: int = 0
    clauses_deleted: int = 0
    restrictions_deleted: int = 0
    doc_ids: list[str] = field(default_factory=list)


@dataclass
class ScopeDeleteResult:
    user_id: str
    scenario_id: str
    documents_deleted: int = 0
    clauses_deleted: int = 0
    restrictions_deleted: int = 0


@dataclass
class ReconcileResult:
    added: int = 0
    updated: int = 0
    relabelled: int = 0
    deleted: int = 0
    unchanged: int = 0
    failed: int = 0
    queued: int = 0
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
        # Attached once built (the queue runs its jobs through this service); without it,
        # reconcile syncs inline.
        self.queue: SyncQueue | None = None

    async def sync_document(
        self,
        doc_id: str,
        *,
        user_id: str | None = None,
        scenario_id: str | None = None,
        replace: bool = False,
    ) -> SyncResult:
        """Ingest a document and extract its restrictions (both idempotent).

        Idempotency guard: on a non-``replace`` sync, if the document is already in the graph
        with the same ``content_hash`` and already has restrictions, the (cheap) ingest still
        runs but the expensive extraction is skipped. This makes a replay of an already-synced
        document — first-boot ``earliest`` backlog, a redelivered event, a retry, or overlap with
        reconcile — a near-no-op instead of a full LLM re-extraction.

        ``user_id``/``scenario_id`` route the document into one IDU_DVD user document index
        instead of the shared corpus — see ``src/ingestion/service.py``.
        """
        prev = None if replace else await self.writer.document_sync_state(doc_id)

        ing = await self.ingestion.ingest_document(
            doc_id, user_id=user_id, scenario_id=scenario_id, replace=replace
        )
        if ing.skipped:
            return SyncResult(doc_id=doc_id, skipped=True, reason=ing.reason)

        unchanged = bool(
            prev
            and not prev.get("extraction_incomplete")
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
            extraction_incomplete=ext.incomplete,
            warnings=ext.warnings,
            failed_clause_ids=ext.failed_clause_ids,
            pruned_clauses=ing.pruned_clauses,
            replaced=replace,
        )
        log.info("document_synced", **asdict(result))
        return result

    async def sync_name(
        self,
        name: str,
        *,
        user_id: str | None = None,
        scenario_id: str | None = None,
        replace: bool = False,
    ) -> list[SyncResult]:
        """Sync every corpus/version entry registered under a document name.

        When ``user_id``/``scenario_id`` are given, ``name`` is resolved within that user
        document index (``DVDClient.resolve_user_doc_ids``) instead of the shared corpus —
        ``GET /library/lookup`` is scope-blind and could otherwise resolve to a same-named
        document belonging to someone else.
        """
        if user_id is not None and scenario_id is not None:
            doc_ids = await self.dvd.resolve_user_doc_ids(user_id, scenario_id, name)
        else:
            doc_ids = await self.dvd.resolve_doc_ids(name)
        if not doc_ids:
            return [SyncResult(name=name, skipped=True, reason=f"unknown name: {name}")]
        results = []
        for doc_id in doc_ids:
            result = await self.sync_document(
                doc_id, user_id=user_id, scenario_id=scenario_id, replace=replace
            )
            result.name = name
            results.append(result)
        return results

    async def delete_name(
        self,
        name: str,
        *,
        user_id: str | None = None,
        scenario_id: str | None = None,
        versions: list[str] | None = None,
        document_removed: bool = True,
    ) -> DeleteResult:
        """Delete a document removed from IDU_DVD.

        When ``document_removed`` is false, only the graph documents whose version matches
        ``versions`` are dropped; otherwise every document under the name is removed.
        ``user_id``/``scenario_id`` narrow the match to one user document index — required so a
        user's document does not delete an official/other-user document sharing the same name.
        """
        stored = await self.writer.documents_by_name(
            name, user_id=user_id, scenario_id=scenario_id
        )
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

    async def delete_scope(self, user_id: str, scenario_id: str) -> ScopeDeleteResult:
        """Wipe a whole user document index's subgraph (all its documents/clauses/restrictions).

        IDU_DVD's ``UserIndexService.delete_index`` wipes Qdrant directly without emitting a
        per-document ``DocumentDeleted`` event, so the Kafka consumer never learns a whole index
        was deleted — this is the explicit admin counterpart (see ``/sync/user-graph`` router).
        """
        counts = await self.writer.delete_scope(user_id, scenario_id)
        result = ScopeDeleteResult(
            user_id=user_id,
            scenario_id=scenario_id,
            documents_deleted=counts.get("documents", 0),
            clauses_deleted=counts.get("clauses", 0),
            restrictions_deleted=counts.get("restrictions", 0),
        )
        log.info("scope_deleted", **asdict(result))
        return result

    async def reconcile(self) -> ReconcileResult:
        """Catch-up pass: align the graph with the current IDU_DVD library listing.

        A document present in DVD but not in the graph is synced; one whose ``content_hash``
        changed is re-synced with ``replace=True``; one in the graph but no longer in DVD is
        deleted. Documents without a ``content_hash`` on the DVD side are treated as unchanged
        (event-driven updates keep them current) to avoid reprocessing on every startup.

        A document whose edition label alone changed (IDU_DVD relabels editions without an
        event: manual edits, the version repair) is re-ingested structurally so provenance
        cites the new label; its restrictions are kept, since its text did not change.

        Documents are visited newest first (by ``uploaded_at``). With a queue attached the
        pass only schedules them — the counters then report what was queued — so the
        catch-up shares one newest-first lane with the Kafka events.
        """
        try:
            listing = await self.dvd.list_library_documents()
        except Exception as exc:  # noqa: BLE001 — reconcile must not break startup
            log.warning("reconcile_dvd_unreachable", error=str(exc))
            return ReconcileResult(skipped=True, reason="dvd unreachable")

        stored = {row["doc_id"]: row for row in await self.writer.stored_documents()}
        result = ReconcileResult()
        seen: set[str] = set()

        documents = sorted(
            listing.documents,
            key=lambda s: changed_at_from_iso(s.uploaded_at),
            reverse=True,
        )
        for summary in documents:
            if not summary.doc_id:
                continue
            seen.add(summary.doc_id)
            action = _reconcile_action(
                stored.get(summary.doc_id), summary.content_hash, summary.version
            )
            if action == "unchanged":
                result.unchanged += 1
                continue
            if self.queue is not None:
                await self.queue.put(
                    SyncJob.for_document(
                        summary.doc_id,
                        changed_at=changed_at_from_iso(summary.uploaded_at),
                        content_hash=summary.content_hash,
                        version=summary.version,
                    )
                )
                result.queued += 1
                _count(result, action)
                continue
            try:
                outcome = await self.reconcile_document(
                    summary.doc_id,
                    content_hash=summary.content_hash,
                    version=summary.version,
                )
            # A single failing document must not abort the whole reconcile pass.
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "reconcile_sync_failed", doc_id=summary.doc_id, error=str(exc)
                )
                outcome = "failed"
            _count(result, outcome)

        for doc_id in set(stored) - seen:
            try:
                await self.writer.delete_document(doc_id)
                result.deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.error("reconcile_delete_failed", doc_id=doc_id, error=str(exc))
                result.failed += 1

        log.info("reconcile_done", **asdict(result))
        return result

    async def reconcile_document(
        self,
        doc_id: str,
        *,
        content_hash: str | None = None,
        version: str | None = None,
    ) -> str:
        """Bring one listed document in line with IDU_DVD; returns its reconcile counter.

        The action is decided from the graph as it is now, not as it was when the document
        was queued, so a document an event already synced in the meantime is left alone.
        """
        prev = await self.writer.stored_document(doc_id)
        action = _reconcile_action(prev, content_hash, version)
        if action == "unchanged":
            return action
        if action == "retry":
            return await self._retry_incomplete(
                doc_id, prev.get("extraction_failed_clause_ids")
            )
        if action == "relabelled":
            await self.ingestion.ingest_document(doc_id)
            return action
        synced = await self.sync_document(doc_id, replace=action == "replace")
        if synced.extraction_incomplete:
            return "failed"
        return "added" if action == "added" else "updated"

    async def _retry_incomplete(self, doc_id: str, failed: list[str] | None) -> str:
        """Finish a document whose last extraction left clauses without a valid result.

        Stale clauses are pruned first: IDU_DVD may have reparsed the document under the same
        hash, and extracting a clause that is no longer in it is wasted work. When that changed
        the structure, or the failed clauses are unknown (the last run was interrupted), the
        whole document is re-extracted; otherwise only the failed clauses are — so a document
        an event has just re-extracted costs a handful of clauses here, not a second pass.
        """
        ing = await self.ingestion.ingest_document(doc_id, replace=True)
        if ing.skipped:
            return "failed"
        if ing.pruned_clauses or not failed:
            ext = await self.extraction.extract_document(doc_id, replace=True)
        else:
            ext = await self.extraction.extract_document(doc_id, clause_ids=failed)
        log.info(
            "document_retried",
            doc_id=doc_id,
            retried_clauses=None if ing.pruned_clauses or not failed else len(failed),
            pruned_clauses=ing.pruned_clauses,
            restrictions=ext.restrictions,
            incomplete=ext.incomplete,
        )
        return "failed" if ext.incomplete else "updated"


def _reconcile_action(
    prev: dict | None, content_hash: str | None, version: str | None
) -> str:
    """What reconcile does to a listed document: added / replace / retry / relabelled /
    unchanged."""
    if prev is None:
        return "added"
    if content_hash and prev.get("content_hash") != content_hash:
        return "replace"
    if prev.get("extraction_incomplete"):
        return "retry"
    if (version or "") != (prev.get("version") or ""):
        return "relabelled"
    return "unchanged"


def _count(result: ReconcileResult, action: str) -> None:
    field_name = {"replace": "updated", "retry": "updated"}.get(action, action)
    setattr(result, field_name, getattr(result, field_name) + 1)

"""Sync / admin router: run the DVD→graph document lifecycle by hand and read sync status.

The event-driven consumer normally keeps the graph in step with IDU_DVD; these endpoints let an
operator force a full reconcile, sync/delete a single document, and see whether the Kafka consumer
is active.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.dependencies import get_dependencies
from src.sync.service import (
    DeleteResult,
    ReconcileResult,
    ScopeDeleteResult,
    SyncResult,
)

sync_router = APIRouter(prefix="/sync", tags=["sync"])


@sync_router.get("/status")
async def sync_status() -> dict:
    """Whether the Kafka consumer is enabled and the sync-related settings in effect."""
    deps = get_dependencies()
    s = deps.settings
    return {
        "kafka_enabled": deps.consumer.enabled,
        "kafka_topic": s.kafka_topic,
        "kafka_group_id": s.kafka_group_id,
        "kafka_bootstrap_servers": s.kafka_bootstrap_servers,
        "reconcile_on_startup": s.reconcile_on_startup,
    }


@sync_router.post("/reconcile")
async def run_reconcile() -> ReconcileResult:
    """Force a full catch-up reconcile against the current IDU_DVD library listing."""
    return await get_dependencies().sync.reconcile()


@sync_router.post("/documents/{doc_id}")
async def sync_document(doc_id: str, replace: bool = False) -> SyncResult:
    """Ingest and extract a single document by ``doc_id`` (``replace`` re-does a changed one)."""
    result = await get_dependencies().sync.sync_document(doc_id, replace=replace)
    if result.skipped and result.reason == "not found in DVD":
        raise HTTPException(status_code=404, detail=result.reason)
    return result


@sync_router.post("/by-name")
async def sync_by_name(name: str, replace: bool = False) -> list[SyncResult]:
    """Ingest and extract every version/corpus entry of a document by name."""
    return await get_dependencies().sync.sync_name(name, replace=replace)


@sync_router.delete("/by-name")
async def delete_by_name(name: str) -> DeleteResult:
    """Remove a document and all its versions from the graph."""
    return await get_dependencies().sync.delete_name(name, document_removed=True)


@sync_router.post("/user-graph")
async def sync_user_graph(
    user_id: str, scenario_id: str, name: str, replace: bool = False
) -> list[SyncResult]:
    """Ingest+extract a document from one IDU_DVD user document index (manual trigger/replay,
    e.g. for testing without Kafka — the consumer normally does this from ``DocumentProcessed``/
    ``DocumentUpdated`` events)."""
    return await get_dependencies().sync.sync_name(
        name, user_id=user_id, scenario_id=scenario_id, replace=replace
    )


@sync_router.delete("/user-graph")
async def delete_user_graph(user_id: str, scenario_id: str) -> ScopeDeleteResult:
    """Wipe a whole user document index's subgraph — manual/backup path.

    As of IDU_DVD branch ``fix/user-index-delete-event``, ``UserIndexService.delete_index``
    emits one scoped ``DocumentDeleted(document_removed=True)`` per document name when a whole
    index is wiped, so the Kafka consumer normally keeps the graph in step on its own (each event
    reaches ``DocumentDeletedHandler`` → ``SyncService.delete_name`` with ``user_id``/
    ``scenario_id``, same as any other deletion). Keep this endpoint for manual cleanup when
    Kafka is disabled/down, for a deployment still running the un-fixed IDU_DVD version, or for
    a stray subgraph left behind by an earlier bug.
    """
    return await get_dependencies().sync.delete_scope(user_id, scenario_id)

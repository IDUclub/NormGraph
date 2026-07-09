"""Ingestion / admin router: trigger ingest of IDU_DVD documents and read graph stats.

Synchronous, operator-facing endpoints for stage 2. The event-driven Kafka sync and the async
job model land in stage 6; for now ingest runs inline and returns the counts.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.dependencies import get_dependencies
from src.ingestion.service import IngestResult

ingestion_router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@ingestion_router.post("/documents/{doc_id}")
async def ingest_document(doc_id: str) -> IngestResult:
    """Ingest a single IDU_DVD document (by ``doc_id``) into the graph."""
    deps = get_dependencies()
    result = await deps.ingestion.ingest_document(doc_id)
    if result.skipped and result.reason == "not found in DVD":
        raise HTTPException(status_code=404, detail=result.reason)
    return result


@ingestion_router.post("/by-name")
async def ingest_by_name(name: str) -> list[IngestResult]:
    """Ingest every version/corpus entry of a document by its name."""
    deps = get_dependencies()
    return await deps.ingestion.ingest_by_name(name)


@ingestion_router.get("/stats")
async def graph_stats() -> dict:
    """Node/edge counts across the restriction graph."""
    deps = get_dependencies()
    return await deps.writer.stats()

"""Extraction router: run restriction extraction over an already-ingested document."""

from __future__ import annotations

from fastapi import APIRouter

from src.dependencies import get_dependencies
from src.dto.extraction import ExtractionBackfillRequest, ExtractionBackfillResponse
from src.pipeline.service import ExtractResult

extraction_router = APIRouter(prefix="/extraction", tags=["extraction"])


@extraction_router.post("/backfill")
async def backfill_extraction(
    request: ExtractionBackfillRequest,
) -> ExtractionBackfillResponse:
    """Extract norms and generate plans for a page of documents with zero restrictions.

    Runs synchronously, one document at a time. Use dry_run to preview document IDs,
    then after_id=next_after_id to continue while has_more is true.
    """
    return await get_dependencies().extraction.backfill(request)


@extraction_router.post("/documents/{doc_id}")
async def extract_document(doc_id: str) -> ExtractResult:
    """Extract restrictions from every clause of a document already present in the graph."""
    deps = get_dependencies()
    return await deps.extraction.extract_document(doc_id)

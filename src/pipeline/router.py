"""Extraction router: run restriction extraction over an already-ingested document."""

from __future__ import annotations

from fastapi import APIRouter

from src.dependencies import get_dependencies
from src.pipeline.service import ExtractResult

extraction_router = APIRouter(prefix="/extraction", tags=["extraction"])


@extraction_router.post("/documents/{doc_id}")
async def extract_document(doc_id: str) -> ExtractResult:
    """Extract restrictions from every clause of a document already present in the graph."""
    deps = get_dependencies()
    return await deps.extraction.extract_document(doc_id)

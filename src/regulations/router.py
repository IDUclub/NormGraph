"""Zone regulations of ПЗЗ documents: what each zone permits and its limit parameters."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.dependencies import get_dependencies
from src.dto.regulations import RegulationDocument, ZoneListResponse

regulations_router = APIRouter(prefix="/regulations", tags=["regulations"])


@regulations_router.get("/zones", response_model=ZoneListResponse)
async def zone_regulations(
    territory_id: list[int] | None = Query(
        None,
        description="Urban API territories the ПЗЗ is tagged with in IDU_DVD (any of them); "
        "pass the scenario's territory with its ancestors",
    ),
    doc_id: str | None = Query(None, description="one ПЗЗ document"),
    code: list[str] | None = Query(None, description="zone codes, e.g. Ж-2.15"),
    vri: str | None = Query(
        None, description="only zones where this ВРИ code is permitted in any section"
    ),
    limit: int = Query(500, ge=1, le=2000),
) -> ZoneListResponse:
    """Zones with their permitted uses (main / conditional / auxiliary) and limit parameters."""
    return await get_dependencies().regulations.zones(
        territory_ids=territory_id, doc_id=doc_id, codes=code, vri_code=vri, limit=limit
    )


@regulations_router.get("/documents", response_model=list[RegulationDocument])
async def regulation_documents(
    territory_id: list[int] | None = Query(None),
) -> list[RegulationDocument]:
    """ПЗЗ documents whose zone regulations were read, with the number of zones."""
    return await get_dependencies().regulations.documents(territory_id)


@regulations_router.post("/documents/{doc_id}/rebuild")
async def rebuild_regulations(doc_id: str) -> dict:
    """Read a document's zone regulations again from IDU_DVD (they are also read on sync)."""
    deps = get_dependencies()
    detail = await deps.dvd.get_document(doc_id)
    if detail is None:
        raise HTTPException(404, f"document not found in IDU_DVD: {doc_id}")
    # IDU_DVD tags the territory after indexing: take the current one along.
    await deps.writer.upsert_document(
        {
            "doc_id": doc_id,
            "territory_id": detail.territory_id,
            "territory_name": detail.territory_name,
            "document_level": detail.document_level,
        }
    )
    return {"doc_id": doc_id, "zones": await deps.regulations.rebuild(detail)}

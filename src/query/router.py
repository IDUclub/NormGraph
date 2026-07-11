"""Query router: restriction search, detail, graph traversal, applicable, and facets."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.dependencies import get_dependencies
from src.dto.query import (
    ApplicableRequest,
    ConflictListResponse,
    EntityOut,
    GraphResponse,
    KindOut,
    RestrictionDetail,
    RestrictionSearchRequest,
    SearchResponse,
)

query_router = APIRouter(tags=["restrictions"])


@query_router.post("/restrictions/search")
async def search_restrictions(req: RestrictionSearchRequest) -> SearchResponse:
    """Search restrictions by text and/or structured filters, with optional neighbours."""
    return await get_dependencies().query.search(req)


@query_router.post("/restrictions/applicable")
async def applicable_restrictions(req: ApplicableRequest) -> SearchResponse:
    """Restrictions applying to a given object/entity (compliance-style query)."""
    return await get_dependencies().query.applicable(req)


@query_router.get("/restrictions/{restriction_id}")
async def get_restriction(restriction_id: str) -> RestrictionDetail:
    """One restriction with full provenance and its direct graph neighbours."""
    detail = await get_dependencies().query.get(restriction_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="restriction not found")
    return detail


@query_router.get("/restrictions/{restriction_id}/graph")
async def traverse_restrictions(
    restriction_id: str,
    depth: int = Query(1, ge=1, description="graph-neighbourhood traversal depth"),
) -> GraphResponse:
    """Traverse the restriction graph from a restriction up to ``depth`` hops."""
    graph = await get_dependencies().query.graph(restriction_id, depth)
    if graph is None:
        raise HTTPException(status_code=404, detail="restriction not found")
    return graph


@query_router.get("/entities")
async def list_entities(
    query: str | None = Query(
        None, description="substring filter on the normalized name"
    ),
    limit: int = Query(50, ge=1, le=500),
) -> list[EntityOut]:
    """Canonical entities (subjects/objects), most-referenced first."""
    return await get_dependencies().query.list_entities(query, limit)


@query_router.get("/restriction-kinds")
async def list_restriction_kinds() -> list[KindOut]:
    """The restriction-kind vocabulary (including auto-added ``pending`` kinds)."""
    return await get_dependencies().query.list_kinds()


@query_router.get("/conflicts")
async def list_conflicts(
    user_id: str | None = Query(None, description="scope to one user document index"),
    scenario_id: str | None = Query(
        None, description="scope to one user document index"
    ),
    restriction_id: str | None = Query(
        None, description="only this restriction's conflicts"
    ),
    limit: int = Query(50, ge=1, le=500),
) -> ConflictListResponse:
    """Possible conflicts (contradicting restriction values) — against the official corpus and/or
    within a user's own upload set, see ``src/pipeline/conflicts.py``."""
    return await get_dependencies().query.list_conflicts(
        user_id, scenario_id, restriction_id, limit
    )

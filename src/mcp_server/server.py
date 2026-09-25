"""MCP server: read-only tools over the restriction graph.

Mounted into the main FastAPI application (``src/main.py``) at ``/mcp`` and reuses the same
``Dependencies`` container as the HTTP routers. The tools mirror the query API so the gMART
orchestrator can reach restrictions over MCP exactly as over REST.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.dependencies import Depends

from src.__version__ import VERSION
from src.common.auth import get_mcp_user_id, service_token_verifier
from src.dependencies import get_dependencies
from src.dto.check_plan import CheckPlanReviewItem, CheckPlanReviewRequest
from src.dto.query import (
    ApplicableRequest,
    ConflictListResponse,
    DocumentListRequest,
    DocumentListResponse,
    EntityResolution,
    EntityResolveRequest,
    GraphResponse,
    RestrictionDetail,
    RestrictionListRequest,
    RestrictionPage,
    RestrictionSearchRequest,
    SearchResponse,
)

mcp = FastMCP("normgraph", auth=service_token_verifier)


@mcp.tool()
def health() -> dict:
    """Liveness probe: confirms the NormGraph MCP server is reachable."""
    return {"service": "normgraph", "version": VERSION, "status": "ok"}


@mcp.tool()
async def search_restrictions(
    query: str | None = None,
    kind: str | None = None,
    kinds: list[str] | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    doc_type: str | None = None,
    corpus: str | None = None,
    lang: str | None = None,
    tags: list[str] | None = None,
    subject: str | None = None,
    object: str | None = None,
    entities: list[str] | None = None,
    limit: int = 10,
    neighbors_depth: int = 0,
) -> SearchResponse:
    """Search normative restrictions by free text and/or structured filters.

    Returns restriction triples {subject, object, kind} + optional value + provenance; set
    ``neighbors_depth`` > 0 to also return the graph neighbourhood of the hits. ``entities``
    keeps restrictions whose subject, object or CheckPlan layer is any of these entities.
    """
    req = RestrictionSearchRequest(
        query=query,
        kind=kind,
        kinds=kinds,
        document_names=document_names,
        version=version,
        doc_type=doc_type,
        corpus=corpus,
        lang=lang,
        tags=tags,
        subject=subject,
        object=object,
        entities=entities,
        limit=limit,
        neighbors_depth=neighbors_depth,
    )
    return await get_dependencies().query.search(req)


@mcp.tool()
async def list_restrictions(
    after_id: str | None = None,
    limit: int = 200,
    executable_only: bool = False,
    kind: str | None = None,
    kinds: list[str] | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    doc_type: str | None = None,
    corpus: str | None = None,
    lang: str | None = None,
    tags: list[str] | None = None,
    subject: str | None = None,
    object: str | None = None,
    entities: list[str] | None = None,
) -> RestrictionPage:
    """Complete listing of restrictions for audits, one keyset page at a time.

    Pages are ordered by restriction id; pass ``next_after_id`` of the previous page as
    ``after_id`` until it is null. ``limit`` is at most 500. ``executable_only`` keeps only
    restrictions whose current CheckPlan is ``auto`` or ``reviewed``. ``entities`` (canonical
    names or aliases, see ``resolve_entities``) keeps restrictions whose subject, object or
    current CheckPlan layer is any of them.
    """
    req = RestrictionListRequest(
        after_id=after_id,
        limit=limit,
        executable_only=executable_only,
        kind=kind,
        kinds=kinds,
        document_names=document_names,
        version=version,
        doc_type=doc_type,
        corpus=corpus,
        lang=lang,
        tags=tags,
        subject=subject,
        object=object,
        entities=entities,
    )
    return await get_dependencies().query.list_page(req)


@mcp.tool()
async def restrictions_applicable(
    object: str,
    subject: str | None = None,
    kind: str | None = None,
    kinds: list[str] | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    entities: list[str] | None = None,
    limit: int = 20,
) -> SearchResponse:
    """Restrictions that apply to a given object/entity (compliance-style check)."""
    req = ApplicableRequest(
        object=object,
        subject=subject,
        kind=kind,
        kinds=kinds,
        document_names=document_names,
        version=version,
        entities=entities,
        limit=limit,
    )
    return await get_dependencies().query.applicable(req)


@mcp.tool()
async def resolve_entities(terms: list[str], limit: int = 10) -> list[EntityResolution]:
    """Candidate canonical entities for free-text topics such as "школы".

    Per term: exact name / alias / stem matches, then check-plan layer names matched the
    same way (``layer`` / ``layer_text``), then nearest entities by embedding (with
    ``score``). Each candidate carries its restriction and executable-restriction counts.
    Pass the chosen ``normalized`` names as ``entities`` to the listing tools.
    """
    req = EntityResolveRequest(terms=terms, limit=limit)
    return await get_dependencies().query.resolve_entities(req)


@mcp.tool()
async def list_restriction_documents(
    executable_only: bool = False,
    entities: list[str] | None = None,
    kind: str | None = None,
    kinds: list[str] | None = None,
    document_names: list[str] | None = None,
    doc_type: str | None = None,
    corpus: str | None = None,
    lang: str | None = None,
    limit: int = 200,
) -> DocumentListResponse:
    """Documents holding matching restrictions, with total and executable counts.

    Ordered by executable count. ``executable_only`` keeps documents that have at least one
    restriction with an ``auto``/``reviewed`` CheckPlan. User-index documents are excluded.
    """
    req = DocumentListRequest(
        executable_only=executable_only,
        entities=entities,
        kind=kind,
        kinds=kinds,
        document_names=document_names,
        doc_type=doc_type,
        corpus=corpus,
        lang=lang,
        limit=limit,
    )
    return await get_dependencies().query.list_documents(req)


@mcp.tool()
async def get_restriction(restriction_id: str) -> RestrictionDetail | None:
    """One restriction with full provenance and its direct graph neighbours."""
    return await get_dependencies().query.get(restriction_id)


@mcp.tool()
async def traverse_restrictions(
    restriction_id: str, depth: int = 1
) -> GraphResponse | None:
    """Traverse the restriction graph from a restriction up to ``depth`` hops."""
    return await get_dependencies().query.graph(restriction_id, depth)


@mcp.tool()
async def list_entities(query: str | None = None, limit: int = 50) -> list:
    """Canonical entities (subjects/objects), most-referenced first."""
    return await get_dependencies().query.list_entities(query, limit)


@mcp.tool()
async def list_restriction_kinds() -> list:
    """The restriction-kind vocabulary, including auto-added pending kinds."""
    return await get_dependencies().query.list_kinds()


@mcp.tool()
async def list_conflicts(
    scenario_id: str | None = None,
    restriction_id: str | None = None,
    limit: int = 50,
    user_id: str = Depends(get_mcp_user_id),
) -> ConflictListResponse:
    """Possible conflicts (contradicting restriction values) between restrictions.

    Set ``user_id``+``scenario_id`` to scope to one user document index (covers both
    conflicts against the official corpus and within the user's own upload set); set
    ``restriction_id`` to only that restriction's conflicts. Intended for a compliance-checking
    caller — every hit needs human/agent review, not automatic resolution.
    """
    return await get_dependencies().query.list_conflicts(
        user_id if scenario_id else None, scenario_id, restriction_id, limit
    )


@mcp.tool()
async def pending_check_plans(limit: int = 100) -> list[CheckPlanReviewItem]:
    """Automatically generated executable plans waiting for expert review."""
    return await get_dependencies().query.pending_check_plans(limit)


@mcp.tool()
async def review_check_plan(
    restriction_id: str,
    action: str,
    plan: dict | None = None,
    reason: str | None = None,
    user_id: str = Depends(get_mcp_user_id),
) -> CheckPlanReviewItem | None:
    """Approve, reject or replace one CheckPlan and append an audit revision."""
    request = CheckPlanReviewRequest(action=action, plan=plan, reason=reason)
    return await get_dependencies().query.review_check_plan(
        restriction_id, request, user_id
    )

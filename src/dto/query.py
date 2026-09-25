"""Request/response models for the restriction query API.

A restriction is returned as its triple + optional ``value`` + full provenance (which clause of
which document/version it was derived from, with source offsets), plus — on the detail/graph
endpoints — its neighbourhood in the restriction graph (restrictions sharing an entity or linked
through a document cross-reference).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.dto.check_plan import CheckPlan
from src.pipeline.models import RestrictionValue


class RestrictionFilters(BaseModel):
    """Structured filters shared by search / applicable (mirrors the IDU_DVD filter set)."""

    kind: str | None = None
    kinds: list[str] | None = None  # any of these kinds (e.g. all placement kinds)
    doc_id: str | None = None
    document_names: list[str] | None = None
    version: str | None = None
    doc_type: str | None = None
    corpus: str | None = None
    lang: str | None = None
    tags: list[str] | None = None
    subject: str | None = None  # matched against the subject entity (normalized/alias)
    object: str | None = None  # matched against the object entity (normalized/alias)
    # Topic filter: any of these entities (normalized name or alias) as the subject, the
    # object or a declared layer of the current CheckPlan.
    entities: list[str] | None = Field(None, max_length=50)


# One response carries full provenance and check plans; larger windows exhaust the
# server's memory. Complete scans page through ``RestrictionListRequest`` instead.
MAX_PAGE_SIZE = 500


class RestrictionSearchRequest(RestrictionFilters):
    query: str | None = None  # free-text query; when omitted, a filtered listing
    limit: int = Field(10, ge=1, le=MAX_PAGE_SIZE)
    neighbors_depth: int = 0  # attach graph neighbourhood up to this depth (0 = none)


class ApplicableRequest(RestrictionFilters):
    """Compliance-style query: which restrictions apply to a given object/entity."""

    object: str  # the object/entity to check (required here)
    subject: str | None = None
    query: str | None = None
    limit: int = Field(20, ge=1, le=MAX_PAGE_SIZE)


class RestrictionListRequest(RestrictionFilters):
    """Complete, stable listing for audits: keyset pages ordered by restriction id."""

    after_id: str | None = None  # ``next_after_id`` of the previous page
    limit: int = Field(200, ge=1, le=MAX_PAGE_SIZE)
    executable_only: bool = False  # only restrictions with an auto/reviewed CheckPlan


class DocumentListRequest(RestrictionFilters):
    """Documents whose restrictions match the filters, e.g. to offer a document choice."""

    executable_only: bool = False  # only documents with an auto/reviewed CheckPlan
    limit: int = Field(200, ge=1, le=MAX_PAGE_SIZE)


class EntityResolveRequest(BaseModel):
    """Free-text topics (\"школы\", \"жилая застройка\") to candidate canonical entities."""

    terms: list[str] = Field(min_length=1, max_length=10)
    limit: int = Field(10, ge=1, le=50)  # candidates per term


class RestrictionProvenance(BaseModel):
    doc_id: str | None = None
    name: str | None = None
    version: str | None = None
    version_id: str | None = None
    doc_type: str | None = None
    corpus: str | None = None
    lang: str | None = None
    clause_node_id: str | None = None
    numbering: str | None = None
    breadcrumb: str | None = None
    char_start: int | None = None
    char_end: int | None = None


class RestrictionOut(BaseModel):
    id: str
    subject: str
    object: str
    kind: str
    kind_status: str = "approved"
    value: RestrictionValue | None = None
    extraction_text: str = ""
    score: float | None = None
    subject_normalized: str | None = None
    object_normalized: str | None = None
    tags: list[str] = Field(default_factory=list)
    provenance: RestrictionProvenance = Field(default_factory=RestrictionProvenance)
    check_plan: CheckPlan | None = None
    check_plan_revision: int | None = None
    check_plan_review_status: str | None = None


class RestrictionNeighbor(BaseModel):
    relation: str  # shares_entity | reference
    restriction: RestrictionOut


class RestrictionDetail(RestrictionOut):
    neighbors: list[RestrictionNeighbor] = Field(default_factory=list)


class DVDHit(BaseModel):
    """A raw IDU_DVD text hit, returned as RAG fallback when the graph has no coverage."""

    doc_id: str
    name: str
    numbering: str = ""
    text: str = ""
    score: float = 0.0


class SearchResponse(BaseModel):
    count: int
    hits: list[RestrictionOut] = Field(default_factory=list)
    neighbors: list[RestrictionNeighbor] = Field(default_factory=list)
    dvd_fallback: list[DVDHit] = Field(default_factory=list)


class RestrictionPage(BaseModel):
    count: int
    hits: list[RestrictionOut] = Field(default_factory=list)
    next_after_id: str | None = None  # None once the listing is exhausted


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str


class GraphResponse(BaseModel):
    root_id: str
    depth: int
    nodes: list[RestrictionOut] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class EntityOut(BaseModel):
    normalized: str
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    status: str = "active"
    restriction_count: int = 0


class EntityCandidate(EntityOut):
    executable_count: int = 0  # restrictions with an auto/reviewed CheckPlan
    match: str  # exact | alias | text | layer | layer_text | vector
    score: float | None = None  # vector similarity, for ``vector`` matches only


class EntityResolution(BaseModel):
    term: str
    candidates: list[EntityCandidate] = Field(default_factory=list)


class DocumentFacet(BaseModel):
    doc_id: str | None = None
    name: str | None = None
    version: str | None = None
    version_id: str | None = None
    doc_type: str | None = None
    corpus: str | None = None
    restriction_count: int = 0
    executable_count: int = 0


class DocumentListResponse(BaseModel):
    count: int
    documents: list[DocumentFacet] = Field(default_factory=list)


class KindOut(BaseModel):
    name: str
    status: str = "approved"
    aliases: list[str] = Field(default_factory=list)
    restriction_count: int = 0


class ConflictOut(BaseModel):
    """One ``CONFLICTS_WITH`` pair — two restrictions whose values are mutually unsatisfiable."""

    restriction: RestrictionOut
    other: RestrictionOut
    reason: str
    severity: str  # "certain" | "possible"


class ConflictListResponse(BaseModel):
    count: int
    conflicts: list[ConflictOut] = Field(default_factory=list)

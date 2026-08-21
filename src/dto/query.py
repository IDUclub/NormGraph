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
    doc_id: str | None = None
    document_names: list[str] | None = None
    version: str | None = None
    doc_type: str | None = None
    corpus: str | None = None
    lang: str | None = None
    tags: list[str] | None = None
    subject: str | None = None  # matched against the subject entity (normalized/alias)
    object: str | None = None  # matched against the object entity (normalized/alias)


class RestrictionSearchRequest(RestrictionFilters):
    query: str | None = None  # free-text query; when omitted, a filtered listing
    limit: int = 10
    neighbors_depth: int = 0  # attach graph neighbourhood up to this depth (0 = none)


class ApplicableRequest(RestrictionFilters):
    """Compliance-style query: which restrictions apply to a given object/entity."""

    object: str  # the object/entity to check (required here)
    subject: str | None = None
    query: str | None = None
    limit: int = 20


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

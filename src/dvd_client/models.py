"""DTOs mirroring the IDU_DVD API surface we consume.

Only the fields NormGraph needs are modelled; unknown fields are ignored (``extra="ignore"``),
so the client keeps working as IDU_DVD grows its payloads.

Note on ``DocumentFragment``: the IDU_DVD ``/library/documents/{doc_id}`` read API does not yet
surface ``references`` / ``child_ids`` (they live in the Qdrant payload but are dropped by the
``DocumentFragment`` DTO on that side). We model them here as optional and forward-compatible: as
soon as IDU_DVD adds them to its library DTO, NormGraph builds the full reference graph from a
single ``get_document`` call. Until then these arrive empty and the edges are back-filled from
search hits (see ``DVDClient.iter_reference_hits``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DocumentRef(BaseModel):
    """Outgoing link from one clause to another document/clause (IDU_DVD contract)."""

    model_config = ConfigDict(extra="ignore")

    raw: str = ""
    target_name: str = ""
    target_numbering: str = ""
    scope: str = "external"  # external | internal
    target_doc_id: str | None = None
    target_version: str | None = None
    target_node_id: str | None = None
    resolved: bool = False


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    doc_id: str
    name: str
    title: str | None = None
    version: str = ""
    version_id: str | None = None
    other_versions: list[str] = Field(default_factory=list)
    doc_type: str = "document"
    corpus: str = "default"
    lang: str | None = None
    status: str = "active"
    external_ids: dict = Field(default_factory=dict)
    source_uri: str | None = None
    content_hash: str | None = None
    node_count: int = 0
    uploaded_at: str | None = None


class DocumentFragment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    order: int = 0
    kind: str = "text"
    type: str = ""
    numbering: str = ""
    depth: int = 0
    breadcrumb: str = ""
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)  # forward-compatible
    char_start: int | None = None
    char_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    span_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    references: list[DocumentRef] = Field(default_factory=list)  # forward-compatible
    text: str = ""
    table_html: str | None = None


class DocumentDetail(DocumentSummary):
    text: str = ""
    fragments: list[DocumentFragment] = Field(default_factory=list)


class DocumentList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: int = 0
    documents: list[DocumentSummary] = Field(default_factory=list)


class UserDocumentInfo(BaseModel):
    """One entry of IDU_DVD's ``GET /user-documents`` aggregated listing (per name+version)."""

    model_config = ConfigDict(extra="ignore")

    doc_id: str
    name: str = ""
    version: str = ""


class UserDocumentList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: int = 0
    documents: list[UserDocumentInfo] = Field(default_factory=list)


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    score: float = 0.0
    doc_id: str
    name: str
    version: str = ""
    versions: list[str] = Field(default_factory=list)
    version_id: str | None = None
    doc_type: str = "document"
    corpus: str = "default"
    lang: str | None = None
    kind: str = "text"
    type: str = ""
    block: str = "main"
    numbering: str = ""
    breadcrumb: str = ""
    depth: int = 0
    order: int = 0
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    span_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    references: list[DocumentRef] = Field(default_factory=list)
    text: str = ""


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: int = 0
    hits: list[SearchHit] = Field(default_factory=list)

"""Ingest a document from IDU_DVD into the graph as :Document + :Clause + edges.

Stage 2 lands the *structural* layer of the graph: documents, clauses, the reading/PART_OF
hierarchy and the REFERENCES edges. Restriction extraction (the :Restriction / :Entity /
:RestrictionKind layer) is added on top in stage 3, keyed off the same clauses.

Reference edges come from ``DocumentFragment.references``. The IDU_DVD library API does not surface
that field yet (see ``src/dvd_client/models.py``); until it does, ``backfill_references=True`` pulls
each clause's references from ``/search`` instead — correct but one search per clause, so it is
opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from src.dvd_client import DVDClient
from src.dvd_client.models import DocumentDetail, DocumentFragment, DocumentRef
from src.graph.writer import GraphWriter

log = structlog.get_logger(__name__)

# Document-summary fields copied onto the :Document node.
_DOC_PROPS = (
    "doc_id",
    "name",
    "title",
    "version",
    "version_id",
    "doc_type",
    "corpus",
    "lang",
    "status",
    "content_hash",
    "source_uri",
    "uploaded_at",
    "node_count",
)


@dataclass
class IngestResult:
    doc_id: str
    clauses: int = 0
    references: int = 0
    pending_references: int = 0
    pruned_clauses: int = 0
    content_hash: str | None = None
    skipped: bool = False
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


class IngestionService:
    def __init__(
        self,
        dvd: DVDClient,
        writer: GraphWriter,
        *,
        backfill_references: bool = False,
    ) -> None:
        self.dvd = dvd
        self.writer = writer
        self.backfill_references = backfill_references

    async def ingest_by_name(
        self, name: str, *, replace: bool = False
    ) -> list[IngestResult]:
        doc_ids = await self.dvd.resolve_doc_ids(name)
        if not doc_ids:
            return [
                IngestResult(doc_id="", skipped=True, reason=f"unknown name: {name}")
            ]
        return [await self.ingest_document(did, replace=replace) for did in doc_ids]

    async def ingest_document(
        self,
        doc_id: str,
        *,
        user_id: str | None = None,
        scenario_id: str | None = None,
        replace: bool = False,
    ) -> IngestResult:
        """Ingest (or re-ingest) a document's structural layer.

        With ``replace=True`` — used when a document changed — clauses dropped by the new version
        are pruned after the upserts, so a re-ingested document does not keep stale clauses.

        ``user_id``/``scenario_id`` stamp the document as belonging to one IDU_DVD user document
        index (see ``src/sync``); reference back-fill is always skipped for a scoped document even
        if ``backfill_references`` is on, mirroring IDU_DVD's own ``enable_reference_linking=False``
        rule for ad hoc user uploads. The intra-document ``PART_OF`` hierarchy (derived from the
        document's own structure, not cross-document resolution) is unaffected.
        """
        detail = await self.dvd.get_document(doc_id)
        if detail is None:
            return IngestResult(doc_id=doc_id, skipped=True, reason="not found in DVD")

        doc_props = self._doc_props(detail)
        if user_id is not None:
            doc_props["user_id"] = user_id
        if scenario_id is not None:
            doc_props["scenario_id"] = scenario_id
        await self.writer.upsert_document(doc_props)

        result = IngestResult(doc_id=doc_id, content_hash=detail.content_hash)
        for frag in detail.fragments:
            await self.writer.upsert_clause(self._clause_props(detail, frag))
            result.clauses += 1

        backfill = self.backfill_references and user_id is None
        # Edges after all clauses exist, so intra-document targets resolve to real nodes.
        for frag in detail.fragments:
            if frag.parent_id:
                await self.writer.link_part_of(frag.id, frag.parent_id)

            refs = await self._references_for(doc_id, frag, backfill=backfill)
            for ref in refs:
                await self.writer.link_reference(frag.id, ref)
                if ref.resolved:
                    result.references += 1
                else:
                    result.pending_references += 1

        if replace:
            keep = [frag.id for frag in detail.fragments]
            result.pruned_clauses = await self.writer.prune_clauses(doc_id, keep)

        log.info(
            "document_ingested",
            doc_id=doc_id,
            name=detail.name,
            clauses=result.clauses,
            references=result.references,
            pending=result.pending_references,
            pruned=result.pruned_clauses,
        )
        return result

    async def _references_for(
        self, doc_id: str, frag: DocumentFragment, *, backfill: bool
    ) -> list[DocumentRef]:
        if frag.references:
            return frag.references
        if not backfill or not frag.text.strip():
            return []
        # Stopgap: find this exact fragment via search and read its references.
        resp = await self.dvd.search(frag.text[:400], doc_id=doc_id, limit=5)
        for hit in resp.hits:
            if hit.id == frag.id:
                return hit.references
        return []

    @staticmethod
    def _doc_props(detail: DocumentDetail) -> dict:
        data = detail.model_dump()
        return {k: data[k] for k in _DOC_PROPS if data.get(k) is not None}

    @staticmethod
    def _clause_props(detail: DocumentDetail, frag: DocumentFragment) -> dict:
        props = {
            "node_id": frag.id,
            "doc_id": detail.doc_id,
            "name": detail.name,
            "version": detail.version,
            "version_id": detail.version_id,
            "kind": frag.kind,
            "type": frag.type,
            "numbering": frag.numbering,
            "breadcrumb": frag.breadcrumb,
            "depth": frag.depth,
            "order": frag.order,
            "char_start": frag.char_start,
            "char_end": frag.char_end,
            "span_id": frag.span_id,
            "tags": frag.tags,
            "text": frag.text,
        }
        return {k: v for k, v in props.items() if v is not None}

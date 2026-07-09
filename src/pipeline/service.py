"""Extraction orchestration: clauses → restrictions → graph.

For each textual clause of a document the service runs the extractor, resolves each triple's kind
and subject/object entities against the graph vocabulary, embeds the restriction, and upserts the
``:Restriction`` node with its ``DERIVED_FROM`` / ``HAS_SUBJECT`` / ``APPLIES_TO`` / ``OF_KIND``
edges, then wires ``SHARES_ENTITY`` links to co-referencing restrictions.

Restriction ids are deterministic (clause + subject + object + kind + value), so re-extracting a
document converges instead of duplicating.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import structlog

from src.graph.writer import GraphWriter
from src.pipeline.extractor import RestrictionExtractor
from src.pipeline.models import ExtractedRestriction, RestrictionValue
from src.pipeline.vocabulary import EntityResolver, KindVocabulary
from src.providers.base import Embedder

log = structlog.get_logger(__name__)


@dataclass
class ExtractResult:
    doc_id: str
    clauses_processed: int = 0
    restrictions: int = 0
    pending_kinds: int = 0
    skipped: bool = False
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _restriction_id(
    clause: str, subject: str, object_: str, kind: str, value: RestrictionValue | None
) -> str:
    value_repr = ""
    if value is not None:
        value_repr = f"{value.operator}|{value.number}|{value.unit}|{value.condition}"
    raw = f"{clause}\x1f{subject}\x1f{object_}\x1f{kind}\x1f{value_repr}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ExtractionService:
    def __init__(
        self,
        writer: GraphWriter,
        extractor: RestrictionExtractor,
        kinds: KindVocabulary,
        entities: EntityResolver,
        embedder: Embedder,
    ) -> None:
        self.writer = writer
        self.extractor = extractor
        self.kinds = kinds
        self.entities = entities
        self.embedder = embedder

    async def extract_document(self, doc_id: str) -> ExtractResult:
        clauses = await self.writer.get_clauses(doc_id)
        if not clauses:
            return ExtractResult(
                doc_id=doc_id, skipped=True, reason="no clauses in graph"
            )

        result = ExtractResult(doc_id=doc_id)
        for clause in clauses:
            extracted = await self.extractor.extract_clause(clause["text"])
            result.clauses_processed += 1
            for ex in extracted:
                pending = await self._write_restriction(doc_id, clause, ex)
                result.restrictions += 1
                if pending:
                    result.pending_kinds += 1

        log.info(
            "document_extracted",
            doc_id=doc_id,
            clauses=result.clauses_processed,
            restrictions=result.restrictions,
            pending_kinds=result.pending_kinds,
        )
        return result

    async def _write_restriction(
        self, doc_id: str, clause: dict, ex: ExtractedRestriction
    ) -> bool:
        """Resolve, embed and upsert one restriction. Returns True if its kind is pending."""
        kind_name, kind_status = await self.kinds.resolve(ex.kind)
        subject_norm = await self.entities.resolve(ex.subject)
        object_norm = await self.entities.resolve(ex.object)

        embed_text = f"{ex.subject} | {ex.object} | {kind_name}"
        if ex.value is not None:
            embed_text += f" | {ex.value.operator or ''}{ex.value.number or ''}{ex.value.unit or ''}"
        embedding = (await self.embedder.embed_documents([embed_text]))[0]

        rid = _restriction_id(
            clause["node_id"], subject_norm, object_norm, kind_name, ex.value
        )
        char_start, char_end = self._absolute_span(clause, ex)
        props = {
            "id": rid,
            "subject": ex.subject,
            "object": ex.object,
            "kind": kind_name,
            "kind_status": kind_status,
            "clause_node_id": clause["node_id"],
            "doc_id": doc_id,
            "version_id": clause.get("version_id"),
            "extraction_text": ex.extraction_text,
        }
        if char_start is not None:
            props["char_start"] = char_start
        if char_end is not None:
            props["char_end"] = char_end
        if ex.value is not None:
            props.update(ex.value.to_props())
        props = {k: v for k, v in props.items() if v is not None}

        await self.writer.upsert_restriction(
            props,
            clause_node_id=clause["node_id"],
            subject_normalized=subject_norm,
            object_normalized=object_norm,
            kind_name=kind_name,
            embedding=embedding,
        )
        await self.writer.link_shares_entity(rid)
        return kind_status == "pending"

    @staticmethod
    def _absolute_span(
        clause: dict, ex: ExtractedRestriction
    ) -> tuple[int | None, int | None]:
        """Map the extraction's clause-relative offsets to absolute source offsets."""
        base = clause.get("char_start")
        if base is None or ex.char_start is None:
            return None, None
        end = base + ex.char_end if ex.char_end is not None else None
        return base + ex.char_start, end

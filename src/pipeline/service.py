"""Extraction orchestration: clauses → restrictions → graph.

For each textual clause of a document the service runs the extractor, resolves each triple's kind
and subject/object entities against the graph vocabulary, embeds the restriction, and upserts the
``:Restriction`` node with its ``DERIVED_FROM`` / ``HAS_SUBJECT`` / ``APPLIES_TO`` / ``OF_KIND``
edges, then wires ``SHARES_ENTITY`` links to co-referencing restrictions.

Restriction ids are deterministic (clause + subject + object + kind + value), so re-extracting a
document converges instead of duplicating.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

import structlog

from src.dto.extraction import (
    ExtractionBackfillItem,
    ExtractionBackfillRequest,
    ExtractionBackfillResponse,
)
from src.graph.writer import GraphWriter
from src.pipeline.check_plan_planner import CheckPlanPlanner
from src.pipeline.conflicts import find_conflicts
from src.pipeline.extractor import RestrictionExtractor
from src.pipeline.models import (
    ExtractedRestriction,
    RestrictionMeasurement,
    RestrictionValue,
)
from src.pipeline.vocabulary import EntityResolver, KindVocabulary
from src.providers.base import Embedder

log = structlog.get_logger(__name__)


@dataclass
class ExtractResult:
    doc_id: str
    clauses_processed: int = 0
    restrictions: int = 0
    pending_kinds: int = 0
    conflicts: int = 0
    replaced: bool = False
    skipped: bool = False
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _restriction_id(
    clause: str,
    subject: str,
    object_: str,
    kind: str,
    value: RestrictionValue | None,
    measurement: RestrictionMeasurement | None = None,
) -> str:
    value_repr = ""
    if value is not None:
        value_repr = f"{value.operator}|{value.number}|{value.unit}|{value.condition}"
    raw = f"{clause}\x1f{subject}\x1f{object_}\x1f{kind}\x1f{value_repr}"
    if measurement is not None:
        raw += "\x1f" + measurement.model_dump_json(
            exclude={"indicator"}, exclude_none=True
        )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ExtractionService:
    def __init__(
        self,
        writer: GraphWriter,
        extractor: RestrictionExtractor,
        kinds: KindVocabulary,
        entities: EntityResolver,
        embedder: Embedder,
        extract_concurrency: int = 64,
        check_plan_planner: CheckPlanPlanner | None = None,
    ) -> None:
        self.writer = writer
        self.extractor = extractor
        self.kinds = kinds
        self.entities = entities
        self.embedder = embedder
        self.extract_concurrency = max(1, int(extract_concurrency))
        self.check_plan_planner = check_plan_planner

    async def backfill(
        self, request: ExtractionBackfillRequest
    ) -> ExtractionBackfillResponse:
        """Recover one page sequentially; normal extraction also generates CheckPlans."""
        rows = await self.writer.documents_without_restrictions(
            after_id=request.after_id, limit=request.limit + 1
        )
        page = rows[: request.limit]
        has_more = len(rows) > request.limit
        result = ExtractionBackfillResponse(
            selected=len(page),
            has_more=has_more,
            next_after_id=page[-1]["doc_id"] if page and has_more else None,
            dry_run=request.dry_run,
        )
        for row in page:
            doc_id = row["doc_id"]
            item = ExtractionBackfillItem(doc_id=doc_id, status="selected")
            result.items.append(item)
            if request.dry_run:
                continue
            try:
                state = await self.writer.document_sync_state(doc_id)
                if state is None or state.get("restrictions", 0) > 0:
                    item.status = "skipped"
                    item.reason = (
                        "document no longer exists"
                        if state is None
                        else "document already has restrictions"
                    )
                else:
                    extracted = await self.extract_document(doc_id)
                    item.status = "skipped" if extracted.skipped else "extracted"
                    item.clauses_processed = extracted.clauses_processed
                    item.restrictions = extracted.restrictions
                    item.reason = extracted.reason
                    result.restrictions += extracted.restrictions
                if item.status == "skipped":
                    result.skipped += 1
                else:
                    result.extracted += 1
            except Exception as exc:  # noqa: BLE001 - isolate failed documents
                item.status = "failed"
                item.reason = str(exc)
                result.failed += 1
                log.warning("extraction_backfill_failed", doc_id=doc_id, error=str(exc))
        log.info("extraction_backfill_completed", **result.model_dump())
        return result

    async def extract_document(
        self, doc_id: str, *, replace: bool = False
    ) -> ExtractResult:
        """Extract restrictions from every clause of an ingested document.

        With ``replace=True`` the document's existing restrictions are dropped first, so a
        re-extraction (e.g. after the source text changed) converges without leaving triples
        that the new text no longer supports.
        """
        clauses = await self.writer.get_clauses(doc_id)
        if not clauses:
            if replace:
                await self.writer.delete_restrictions_of_doc(doc_id)
            return ExtractResult(
                doc_id=doc_id, skipped=True, reason="no clauses in graph"
            )

        if replace:
            await self.writer.delete_restrictions_of_doc(doc_id)

        semaphore = asyncio.Semaphore(self.extract_concurrency)

        async def extract(clause):
            async with semaphore:
                return clause, await self.extractor.extract_clause(clause["text"])

        # Only the independent LLM extraction is parallel. Graph vocabulary resolution and
        # writes remain ordered below, avoiding races while preserving input clause order.
        clause_results = await asyncio.gather(*(extract(clause) for clause in clauses))

        result = ExtractResult(doc_id=doc_id, replaced=replace)
        for clause, extracted in clause_results:
            result.clauses_processed += 1
            for ex in extracted:
                pending, conflicts = await self._write_restriction(
                    doc_id, clause, ex, warnings=result.warnings
                )
                result.restrictions += 1
                if pending:
                    result.pending_kinds += 1
                result.conflicts += conflicts

        log.info(
            "document_extracted",
            doc_id=doc_id,
            clauses=result.clauses_processed,
            restrictions=result.restrictions,
            pending_kinds=result.pending_kinds,
            conflicts=result.conflicts,
        )
        return result

    async def _write_restriction(
        self,
        doc_id: str,
        clause: dict,
        ex: ExtractedRestriction,
        *,
        warnings: list[str] | None = None,
    ) -> tuple[bool, int]:
        """Resolve, embed and upsert one restriction.

        Returns ``(kind_is_pending, conflicts_found)``. Conflict detection runs against this
        restriction's ``SHARES_ENTITY`` neighbours (see ``src/pipeline/conflicts.py``) — these span
        both the official corpus and the rest of a user's own upload set, since both resolve into
        the same shared entity/kind vocabulary.
        """
        kind_name, kind_status = await self.kinds.resolve(ex.kind)
        subject_norm = await self.entities.resolve(ex.subject)
        object_norm = await self.entities.resolve(ex.object)

        embed_text = f"{ex.subject} | {ex.object} | {kind_name}"
        if ex.value is not None:
            embed_text += f" | {ex.value.operator or ''}{ex.value.number or ''}{ex.value.unit or ''}"
        embedding = (await self.embedder.embed_documents([embed_text]))[0]

        rid = _restriction_id(
            clause["node_id"],
            subject_norm,
            object_norm,
            kind_name,
            ex.value,
            ex.measurement,
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
            "measurement_json": (
                ex.measurement.model_dump_json() if ex.measurement else None
            ),
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
        if self.check_plan_planner is not None:
            try:
                check_plan = await self.check_plan_planner.plan(rid, ex)
            except (
                Exception
            ) as exc:  # isolate planning; graph/storage failures still propagate
                log.warning(
                    "restriction_plan_failed", restriction_id=rid, error=str(exc)
                )
                check_plan = CheckPlanPlanner.unsupported_plan(
                    rid, ex, reasons=["planner_failed"]
                )
                if warnings is not None:
                    warnings.append(f"{rid}: planner_failed")
            await self.writer.append_check_plan_revision(
                rid,
                check_plan.model_dump(mode="json"),
                review_status=(
                    "pending" if check_plan.planner_status == "auto" else "rejected"
                ),
                protect_reviewed=True,
            )
        neighbors = await self.writer.link_shares_entity(rid)
        conflicts = find_conflicts(rid, kind_name, ex.value, neighbors)
        for c in conflicts:
            await self.writer.upsert_conflict(
                rid, c.other_id, reason=c.reason, severity=c.severity
            )
        return kind_status == "pending", len(conflicts)

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

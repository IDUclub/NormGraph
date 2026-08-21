"""Query orchestration: embeds the query, reads the graph, assembles the response DTOs.

Search runs vector similarity over the restriction embeddings and applies structured filters; when
a text query yields nothing and the graph lacks coverage, it falls back to IDU_DVD ``/search`` so
the caller still gets grounded source snippets. Detail and graph endpoints expand the restriction
neighbourhood (restrictions sharing an entity, or linked through a document cross-reference).
"""

from __future__ import annotations

import json

import structlog

from src.common.config import Settings
from src.dto.check_plan import (
    CheckPlan,
    CheckPlanReviewItem,
    CheckPlanReviewRequest,
    validate_check_plan,
)
from src.dto.query import (
    ApplicableRequest,
    ConflictListResponse,
    ConflictOut,
    DVDHit,
    EntityOut,
    GraphEdge,
    GraphResponse,
    KindOut,
    RestrictionDetail,
    RestrictionNeighbor,
    RestrictionOut,
    RestrictionProvenance,
    RestrictionSearchRequest,
    SearchResponse,
)
from src.dvd_client import DVDClient
from src.graph.reader import GraphReader
from src.graph.writer import GraphWriter
from src.pipeline.models import RestrictionValue
from src.pipeline.vocabulary import normalize
from src.providers.base import Embedder

log = structlog.get_logger(__name__)


def _to_out(row: dict) -> RestrictionOut:
    value = RestrictionValue(
        operator=row.get("value_operator"),
        number=row.get("value_number"),
        unit=row.get("value_unit"),
        condition=row.get("value_condition"),
    )
    check_plan = None
    if row.get("check_schema_version"):
        check_plan = CheckPlan.model_validate(
            {
                "schema_version": row["check_schema_version"],
                "template": row["check_template"],
                "template_version": row["check_template_version"],
                "params": json.loads(row.get("check_params_json") or "{}"),
                "declared_requirements": json.loads(
                    row.get("check_requirements_json") or "null"
                ),
                "source": json.loads(row.get("check_source_json") or "{}"),
                "planner_status": row["check_planner_status"],
            }
        )
    return RestrictionOut(
        id=row["id"],
        subject=row.get("subject") or "",
        object=row.get("object") or "",
        kind=row.get("kind") or "",
        kind_status=row.get("kind_status") or "approved",
        value=None if value.is_empty() else value,
        extraction_text=row.get("extraction_text") or "",
        score=row.get("score"),
        subject_normalized=row.get("subject_normalized"),
        object_normalized=row.get("object_normalized"),
        tags=row.get("tags") or [],
        provenance=RestrictionProvenance(
            doc_id=row.get("doc_id"),
            name=row.get("name"),
            version=row.get("version"),
            version_id=row.get("version_id"),
            doc_type=row.get("doc_type"),
            corpus=row.get("corpus"),
            lang=row.get("lang"),
            clause_node_id=row.get("clause_node_id"),
            numbering=row.get("numbering"),
            breadcrumb=row.get("breadcrumb"),
            char_start=row.get("char_start"),
            char_end=row.get("char_end"),
        ),
        check_plan=check_plan,
        check_plan_revision=row.get("check_revision"),
        check_plan_review_status=row.get("check_review_status"),
    )


class QueryService:
    def __init__(
        self,
        reader: GraphReader,
        embedder: Embedder,
        dvd: DVDClient,
        settings: Settings,
        writer: GraphWriter | None = None,
    ) -> None:
        self.reader = reader
        self.embedder = embedder
        self.dvd = dvd
        self.settings = settings
        self.writer = writer

    @staticmethod
    def _review_item(row: dict) -> CheckPlanReviewItem:
        plan = CheckPlan.model_validate(
            {
                "schema_version": row["schema_version"],
                "template": row["template"],
                "template_version": row["template_version"],
                "params": json.loads(row.get("params_json") or "{}"),
                "declared_requirements": json.loads(
                    row.get("requirements_json") or "null"
                ),
                "source": json.loads(row.get("source_json") or "{}"),
                "planner_status": row["planner_status"],
            }
        )
        return CheckPlanReviewItem(
            restriction_id=row["restriction_id"],
            plan=plan,
            revision=row["revision"],
            review_status=row.get("review_status") or "pending",
            author=row.get("author"),
            reason=row.get("reason"),
            created_at=row.get("created_at"),
            current=bool(row.get("current")),
        )

    async def pending_check_plans(self, limit: int = 100) -> list[CheckPlanReviewItem]:
        return [
            self._review_item(row)
            for row in await self.reader.pending_check_plans(limit)
        ]

    async def check_plan_revisions(
        self, restriction_id: str
    ) -> list[CheckPlanReviewItem]:
        return [
            self._review_item(row)
            for row in await self.reader.check_plan_revisions(restriction_id)
        ]

    async def review_check_plan(
        self,
        restriction_id: str,
        request: CheckPlanReviewRequest,
        author: str,
    ) -> CheckPlanReviewItem | None:
        if self.writer is None:
            raise RuntimeError("check-plan review writer is unavailable")
        revisions = await self.check_plan_revisions(restriction_id)
        if not revisions:
            return None
        current = next((item for item in revisions if item.current), revisions[0])
        if request.action == "replace":
            if request.plan is None:
                raise ValueError("plan is required for replace")
            plan = validate_check_plan(request.plan.model_dump(mode="json"))
        else:
            plan = current.plan.model_copy(deep=True)
        if request.action == "reject":
            plan.planner_status = "unsupported"
            review_status = "rejected"
        else:
            plan.planner_status = "reviewed"
            review_status = "approved"
        await self.writer.append_check_plan_revision(
            restriction_id,
            plan.model_dump(mode="json"),
            review_status=review_status,
            author=author,
            reason=request.reason,
        )
        updated = await self.check_plan_revisions(restriction_id)
        return updated[0] if updated else None

    def _filters(self, req) -> dict:
        return {
            "kind": req.kind,
            "doc_id": req.doc_id,
            "document_names": req.document_names,
            "version": req.version,
            "doc_type": req.doc_type,
            "corpus": req.corpus,
            "lang": req.lang,
            "tags": req.tags,
            "subject": normalize(req.subject) if req.subject else None,
            "object": normalize(req.object) if req.object else None,
        }

    async def search(self, req: RestrictionSearchRequest) -> SearchResponse:
        filters = self._filters(req)
        if req.query:
            vec = await self.embedder.embed_query(req.query)
            rows = await self.reader.search_vector(
                self.settings.restriction_vector_index, vec, filters, limit=req.limit
            )
        else:
            rows = await self.reader.search_filter(filters, limit=req.limit)

        hits = [_to_out(r) for r in rows]

        neighbors: list[RestrictionNeighbor] = []
        if hits and req.neighbors_depth > 0:
            neighbors = await self._expand([h.id for h in hits], req.neighbors_depth)

        dvd_fallback: list[DVDHit] = []
        if not hits and req.query and self.settings.dvd_search_fallback:
            dvd_fallback = await self._dvd_fallback(req)

        return SearchResponse(
            count=len(hits),
            hits=hits,
            neighbors=neighbors,
            dvd_fallback=dvd_fallback,
        )

    async def get(self, restriction_id: str) -> RestrictionDetail | None:
        rows = await self.reader.get_by_ids([restriction_id])
        if not rows:
            return None
        out = _to_out(rows[0])
        neighbors = await self._neighbors_of([restriction_id])
        return RestrictionDetail(**out.model_dump(), neighbors=neighbors)

    async def graph(self, restriction_id: str, depth: int) -> GraphResponse | None:
        depth = max(1, min(depth, self.settings.max_traversal_depth))
        rows = await self.reader.get_by_ids([restriction_id])
        if not rows:
            return None

        visited = {restriction_id}
        edges_seen: set[tuple[str, str, str]] = set()
        edges: list[GraphEdge] = []
        frontier = [restriction_id]
        for _ in range(depth):
            if not frontier:
                break
            nb_rows = await self.reader.neighbors(frontier)
            next_frontier: list[str] = []
            for row in nb_rows:
                src, dst, rel = row["src"], row["neighbor_id"], row["relation"]
                key = (min(src, dst), max(src, dst), rel)
                if key not in edges_seen:
                    edges_seen.add(key)
                    edges.append(GraphEdge(source=src, target=dst, relation=rel))
                if dst not in visited:
                    visited.add(dst)
                    next_frontier.append(dst)
            frontier = next_frontier

        node_rows = await self.reader.get_by_ids(list(visited))
        return GraphResponse(
            root_id=restriction_id,
            depth=depth,
            nodes=[_to_out(r) for r in node_rows],
            edges=edges,
        )

    async def applicable(self, req: ApplicableRequest) -> SearchResponse:
        targets = {normalize(req.object)}
        vec = (await self.embedder.embed_documents([normalize(req.object)]))[0]
        near = await self.reader.nearest_entities(
            self.settings.entity_vector_index, vec, k=5
        )
        for item in near:
            if item.get("score", 0.0) >= self.settings.entity_merge_threshold:
                targets.add(item["normalized"])

        filters = self._filters(req)
        rows = await self.reader.applicable(
            [t for t in targets if t], filters, limit=req.limit
        )
        hits = [_to_out(r) for r in rows]
        return SearchResponse(count=len(hits), hits=hits)

    async def list_entities(
        self, query: str | None = None, limit: int = 50
    ) -> list[EntityOut]:
        rows = await self.reader.list_entities(query, limit=limit)
        return [EntityOut(**r) for r in rows]

    async def list_kinds(self) -> list[KindOut]:
        rows = await self.reader.list_kinds()
        return [KindOut(**r) for r in rows]

    async def list_conflicts(
        self,
        user_id: str | None = None,
        scenario_id: str | None = None,
        restriction_id: str | None = None,
        limit: int = 50,
    ) -> ConflictListResponse:
        """Possible conflicts (contradicting restriction values), scoped to a user index and/or
        one restriction — see ``src/pipeline/conflicts.py`` for how they are detected.
        """
        pairs = await self.reader.conflict_pairs(
            user_id=user_id,
            scenario_id=scenario_id,
            restriction_id=restriction_id,
            limit=limit,
        )
        if not pairs:
            return ConflictListResponse(count=0, conflicts=[])

        ids = {p["restriction_id"] for p in pairs} | {p["other_id"] for p in pairs}
        rows = await self.reader.get_by_ids(list(ids))
        by_id = {r["id"]: _to_out(r) for r in rows}

        conflicts = [
            ConflictOut(
                restriction=by_id[p["restriction_id"]],
                other=by_id[p["other_id"]],
                reason=p.get("reason") or "",
                severity=p.get("severity") or "possible",
            )
            for p in pairs
            if p["restriction_id"] in by_id and p["other_id"] in by_id
        ]
        return ConflictListResponse(count=len(conflicts), conflicts=conflicts)

    # --- helpers ---------------------------------------------------------------------

    async def _neighbors_of(self, ids: list[str]) -> list[RestrictionNeighbor]:
        nb_rows = await self.reader.neighbors(ids)
        by_id = {row["neighbor_id"]: row["relation"] for row in nb_rows}
        if not by_id:
            return []
        detail_rows = await self.reader.get_by_ids(list(by_id))
        return [
            RestrictionNeighbor(relation=by_id[r["id"]], restriction=_to_out(r))
            for r in detail_rows
        ]

    async def _expand(self, ids: list[str], depth: int) -> list[RestrictionNeighbor]:
        depth = max(1, min(depth, self.settings.max_traversal_depth))
        visited = set(ids)
        collected: dict[str, str] = {}
        frontier = list(ids)
        for _ in range(depth):
            if not frontier:
                break
            nb_rows = await self.reader.neighbors(frontier)
            next_frontier = []
            for row in nb_rows:
                dst, rel = row["neighbor_id"], row["relation"]
                if dst not in visited:
                    visited.add(dst)
                    collected.setdefault(dst, rel)
                    next_frontier.append(dst)
            frontier = next_frontier
        if not collected:
            return []
        detail_rows = await self.reader.get_by_ids(list(collected))
        return [
            RestrictionNeighbor(relation=collected[r["id"]], restriction=_to_out(r))
            for r in detail_rows
        ]

    async def _dvd_fallback(self, req: RestrictionSearchRequest) -> list[DVDHit]:
        try:
            resp = await self.dvd.search(
                req.query,
                document_names=req.document_names,
                version=req.version,
                tags=req.tags,
                limit=req.limit,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("dvd_fallback_failed", error=str(exc))
            return []
        return [
            DVDHit(
                doc_id=h.doc_id,
                name=h.name,
                numbering=h.numbering,
                text=h.text,
                score=h.score,
            )
            for h in resp.hits
        ]

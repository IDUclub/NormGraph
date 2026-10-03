"""Write helpers for the ingestion path.

All writes are idempotent (``MERGE`` on the natural key + ``SET += props``), so re-ingesting a
document or ingesting documents out of order converges to the same graph. A referenced clause that
is not in the store yet is ``MERGE``-d as a thin ``:Clause`` node and filled in when its own
document is later ingested; an unresolved reference lands on a ``:PendingReference`` stub.
"""

from __future__ import annotations

import json

import structlog

from src.dvd_client.models import DocumentRef
from src.graph.client import Neo4jClient
from src.graph.context import load_clause_contexts
from src.pipeline.clause_context import ClauseContext
from src.pipeline.vocabulary import layer_entity_keys

log = structlog.get_logger(__name__)


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _stored_requirements(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw or "null")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class GraphWriter:
    def __init__(self, client: Neo4jClient) -> None:
        self.client = client

    async def upsert_document(self, props: dict) -> None:
        await self.client.run(
            "MERGE (d:Document {doc_id: $doc_id}) SET d += $props",
            doc_id=props["doc_id"],
            props=props,
        )

    async def upsert_clause(self, props: dict) -> None:
        """Upsert a clause and attach it to its document."""
        await self.client.run(
            """
            MERGE (c:Clause {node_id: $node_id})
            SET c += $props
            WITH c
            MATCH (d:Document {doc_id: $doc_id})
            MERGE (c)-[:IN_DOCUMENT]->(d)
            """,
            node_id=props["node_id"],
            props=props,
            doc_id=props["doc_id"],
        )

    async def link_part_of(self, child_node_id: str, parent_node_id: str) -> None:
        await self.client.run(
            """
            MATCH (c:Clause {node_id: $child})
            MERGE (p:Clause {node_id: $parent})
            MERGE (c)-[:PART_OF]->(p)
            """,
            child=child_node_id,
            parent=parent_node_id,
        )

    async def replace_dependencies(self, doc_id: str, rows: list[dict]) -> int:
        """Replace the document's ``DEPENDS_ON`` edges (``[{source, target, weight, kind}]``).

        ``(a)-[:DEPENDS_ON]->(b)``: applying clause ``a`` needs reading ``b`` (IDU_DVD's
        fragment relations). Only edges between clauses already in the graph are written.
        """
        await self.client.run(
            """
            MATCH (a:Clause)-[e:DEPENDS_ON]->(:Clause)
            WHERE (a)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})
            DELETE e
            """,
            doc_id=doc_id,
        )
        if not rows:
            return 0
        written = await self.client.run(
            """
            UNWIND $rows AS row
            MATCH (a:Clause {node_id: row.source})
            MATCH (b:Clause {node_id: row.target})
            MERGE (a)-[e:DEPENDS_ON]->(b)
            SET e.weight = row.weight, e.kind = row.kind
            RETURN count(e) AS n
            """,
            rows=rows,
        )
        return written[0]["n"] if written else 0

    async def link_reference(self, src_node_id: str, ref: DocumentRef) -> None:
        """Create a REFERENCES edge, choosing the target by how far it resolved."""
        if ref.resolved and ref.target_node_id:
            await self.client.run(
                """
                MATCH (s:Clause {node_id: $src})
                MERGE (t:Clause {node_id: $tid})
                MERGE (s)-[r:REFERENCES]->(t)
                SET r.scope = $scope, r.resolved = true, r.raw = $raw,
                    r.target_numbering = $tnum
                """,
                src=src_node_id,
                tid=ref.target_node_id,
                scope=ref.scope,
                raw=ref.raw,
                tnum=ref.target_numbering,
            )
        elif ref.resolved and ref.target_doc_id:
            await self.client.run(
                """
                MATCH (s:Clause {node_id: $src})
                MERGE (t:Document {doc_id: $tdoc})
                MERGE (s)-[r:REFERENCES]->(t)
                SET r.scope = $scope, r.resolved = true, r.raw = $raw,
                    r.target_numbering = $tnum
                """,
                src=src_node_id,
                tdoc=ref.target_doc_id,
                scope=ref.scope,
                raw=ref.raw,
                tnum=ref.target_numbering,
            )
        else:
            key = f"{_norm(ref.target_name)}#{ref.target_numbering}"
            await self.client.run(
                """
                MATCH (s:Clause {node_id: $src})
                MERGE (p:PendingReference {key: $key})
                SET p.target_name = $name, p.target_numbering = $tnum
                MERGE (s)-[r:REFERENCES]->(p)
                SET r.scope = $scope, r.resolved = false, r.raw = $raw
                """,
                src=src_node_id,
                key=key,
                name=ref.target_name,
                tnum=ref.target_numbering,
                scope=ref.scope,
                raw=ref.raw,
            )

    # --- restriction / entity / kind layer (stage 3) ---------------------------------

    async def nearest(
        self, index_name: str, embedding: list[float], k: int = 1
    ) -> list[dict]:
        """Top-k nearest nodes in a vector index, with cosine score."""
        return await self.client.run(
            """
            CALL db.index.vector.queryNodes($index, $k, $vec)
            YIELD node, score
            RETURN node.name AS name, node.normalized AS normalized,
                   node.status AS status, score
            """,
            index=index_name,
            k=k,
            vec=embedding,
        )

    async def get_entity(self, normalized: str) -> dict | None:
        """Exact entity lookup by normalized key or alias."""
        rows = await self.client.run(
            """
            MATCH (e:Entity)
            WHERE e.normalized = $norm OR $norm IN coalesce(e.aliases, [])
            RETURN e.normalized AS normalized, e.name AS name
            LIMIT 1
            """,
            norm=normalized,
        )
        return rows[0] if rows else None

    async def ensure_kind(
        self,
        name: str,
        *,
        status: str = "approved",
        aliases: list[str] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        await self.client.run(
            """
            MERGE (k:RestrictionKind {name: $name})
            ON CREATE SET k.status = $status, k.aliases = $aliases
            SET k.aliases = coalesce(k.aliases, []) + [a IN $aliases
                            WHERE NOT a IN coalesce(k.aliases, [])]
            FOREACH (_ IN CASE WHEN $embedding IS NULL THEN [] ELSE [1] END |
                     SET k.embedding = $embedding)
            """,
            name=name,
            status=status,
            aliases=aliases or [],
            embedding=embedding,
        )

    async def upsert_entity(
        self,
        normalized: str,
        *,
        name: str,
        aliases: list[str] | None = None,
        embedding: list[float] | None = None,
        status: str = "active",
    ) -> None:
        await self.client.run(
            """
            MERGE (e:Entity {normalized: $normalized})
            ON CREATE SET e.name = $name, e.status = $status
            SET e.aliases = coalesce(e.aliases, []) + [a IN $aliases
                            WHERE NOT a IN coalesce(e.aliases, [])]
            FOREACH (_ IN CASE WHEN $embedding IS NULL THEN [] ELSE [1] END |
                     SET e.embedding = $embedding)
            """,
            normalized=normalized,
            name=name,
            aliases=aliases or [],
            embedding=embedding,
            status=status,
        )

    async def upsert_restriction(
        self,
        props: dict,
        *,
        clause_node_id: str,
        subject_normalized: str,
        object_normalized: str,
        kind_name: str,
        embedding: list[float] | None = None,
    ) -> None:
        """Upsert a restriction node and wire it to its clause, entities and kind."""
        await self.client.run(
            """
            MERGE (r:Restriction {id: $id})
            SET r += $props
            FOREACH (_ IN CASE WHEN $embedding IS NULL THEN [] ELSE [1] END |
                     SET r.embedding = $embedding)
            WITH r
            MATCH (c:Clause {node_id: $clause})
            MERGE (r)-[:DERIVED_FROM]->(c)
            WITH r
            MATCH (s:Entity {normalized: $subject})
            MERGE (r)-[:HAS_SUBJECT]->(s)
            WITH r
            MATCH (o:Entity {normalized: $object})
            MERGE (r)-[:APPLIES_TO]->(o)
            WITH r
            MATCH (k:RestrictionKind {name: $kind})
            MERGE (r)-[:OF_KIND]->(k)
            WITH r
            OPTIONAL MATCH (saved:CheckPlan {restriction_id: $id})
            FOREACH (_ IN CASE WHEN saved IS NULL THEN [] ELSE [1] END |
                     MERGE (r)-[:HAS_CHECK_PLAN]->(saved))
            """,
            id=props["id"],
            props=props,
            embedding=embedding,
            clause=clause_node_id,
            subject=subject_normalized,
            object=object_normalized,
            kind=kind_name,
        )

    async def set_restriction_embeddings(
        self, rows: list[dict], *, version: int
    ) -> None:
        """Replace stored vectors (``[{id, embedding}]``) and record their text version."""
        await self.client.run(
            """
            UNWIND $rows AS row
            MATCH (r:Restriction {id: row.id})
            SET r.embedding = row.embedding, r.embedding_version = $version
            """,
            rows=rows,
            version=version,
        )

    async def append_check_plan_revision(
        self,
        restriction_id: str,
        plan: dict,
        *,
        review_status: str,
        author: str | None = None,
        reason: str | None = None,
        protect_reviewed: bool = False,
        skip_if_current: bool = False,
        expected_revision: int | None = None,
        planner_version: int | None = None,
        trace: dict | None = None,
    ) -> int | None:
        """Append an immutable plan revision and atomically make it current.

        ``planner_version`` marks automatic revisions (an expert revision has none);
        ``trace`` records the planner passes behind it.
        """

        rows = await self.client.run(
            """
            MATCH (r:Restriction {id: $restriction_id})
            SET r.check_plan_write_lock = coalesce(r.check_plan_write_lock, 0) + 1
            WITH r
            OPTIONAL MATCH (current:CheckPlan {
                restriction_id: $restriction_id, current: true
            })
            WITH r, current
            WHERE (NOT $skip_if_current OR current IS NULL)
              AND (NOT $protect_reviewed
                   OR current IS NULL
                   OR current.planner_status <> 'reviewed')
              AND ($expected_revision IS NULL
                   OR coalesce(current.revision, 0) = $expected_revision)
              AND ($expected_revision IS NULL
                   OR current.author IS NULL)
            WITH r, current, coalesce(current.revision, 0) + 1 AS revision
            FOREACH (_ IN CASE WHEN current IS NULL THEN [] ELSE [1] END |
                     SET current.current = false)
            CREATE (plan:CheckPlan {
                restriction_id: $restriction_id,
                revision: revision,
                current: true,
                schema_version: $schema_version,
                template: $template,
                template_version: $template_version,
                params_json: $params_json,
                requirements_json: $requirements_json,
                layer_entities: $layer_entities,
                source_json: $source_json,
                planner_status: $planner_status,
                review_status: $review_status,
                author: $author,
                reason: $reason,
                planner_version: $planner_version,
                trace_json: $trace_json,
                created_at: datetime()
            })
            MERGE (r)-[:HAS_CHECK_PLAN]->(plan)
            RETURN revision
            """,
            restriction_id=restriction_id,
            protect_reviewed=protect_reviewed,
            skip_if_current=skip_if_current,
            expected_revision=expected_revision,
            schema_version=plan["schema_version"],
            template=plan["template"],
            template_version=plan["template_version"],
            params_json=json.dumps(plan.get("params") or {}, ensure_ascii=False),
            requirements_json=json.dumps(
                plan.get("declared_requirements"), ensure_ascii=False
            ),
            layer_entities=layer_entity_keys(plan.get("declared_requirements")),
            source_json=json.dumps(plan["source"], ensure_ascii=False),
            planner_status=plan["planner_status"],
            review_status=review_status,
            author=author,
            reason=reason,
            planner_version=planner_version,
            trace_json=(
                json.dumps(trace, ensure_ascii=False) if trace is not None else None
            ),
        )
        return int(rows[0]["revision"]) if rows else None

    async def backfill_check_plan_layer_entities(self, *, batch: int = 500) -> int:
        """Key plans stored before ``layer_entities`` existed; returns how many were set.

        Cypher cannot parse ``requirements_json`` without APOC, so the labels are
        normalized here. Every processed plan gets a list (possibly empty), so the
        loop always makes progress and a second run touches nothing.
        """
        updated = 0
        while True:
            rows = await self.client.run(
                """
                MATCH (cp:CheckPlan)
                WHERE cp.layer_entities IS NULL
                RETURN elementId(cp) AS element_id,
                       cp.requirements_json AS requirements_json
                LIMIT $batch
                """,
                batch=batch,
            )
            if not rows:
                return updated
            await self.client.run(
                """
                UNWIND $rows AS row
                MATCH (cp:CheckPlan) WHERE elementId(cp) = row.element_id
                SET cp.layer_entities = row.layer_entities
                """,
                rows=[
                    {
                        "element_id": row["element_id"],
                        "layer_entities": layer_entity_keys(
                            _stored_requirements(row.get("requirements_json"))
                        ),
                    }
                    for row in rows
                ],
            )
            updated += len(rows)

    async def link_shares_entity(self, restriction_id: str) -> list[dict]:
        """Connect a restriction to every other restriction sharing a subject/object entity.

        Returns the neighbour rows (id + kind + value fields), not just a count, so the caller
        can run conflict detection over them without a second round trip.
        """
        rows = await self.client.run(
            """
            MATCH (r:Restriction {id: $id})-[:HAS_SUBJECT|APPLIES_TO]->(e:Entity)
                  <-[:HAS_SUBJECT|APPLIES_TO]-(other:Restriction)
            WHERE other.id <> r.id
            MERGE (r)-[:SHARES_ENTITY]-(other)
            RETURN DISTINCT other.id AS id, other.kind AS kind, other.doc_id AS doc_id,
                   other.value_operator AS value_operator, other.value_number AS value_number,
                   other.value_unit AS value_unit, other.value_condition AS value_condition
            """,
            id=restriction_id,
        )
        return rows

    async def upsert_conflict(
        self, restriction_id: str, other_id: str, *, reason: str, severity: str
    ) -> None:
        """Merge a ``CONFLICTS_WITH`` edge between two restrictions (undirected, deduped)."""
        await self.client.run(
            """
            MATCH (r:Restriction {id: $rid}), (o:Restriction {id: $oid})
            MERGE (r)-[c:CONFLICTS_WITH]-(o)
            SET c.reason = $reason, c.severity = $severity, c.detected_at = datetime()
            """,
            rid=restriction_id,
            oid=other_id,
            reason=reason,
            severity=severity,
        )

    async def duplicate_candidates(
        self, norm_key: str, restriction_id: str, *, doc_id: str
    ) -> list[dict]:
        """Other restrictions of the shared corpus stating the norm ``norm_key``.

        A user's own document is not grouped: its duplicates would show other scopes.
        """
        return await self.client.run(
            """
            MATCH (own:Document {doc_id: $doc_id}) WHERE own.user_id IS NULL
            MATCH (r:Restriction {norm_key: $key})-[:DERIVED_FROM]->(:Clause)
                  -[:IN_DOCUMENT]->(d:Document)
            WHERE r.id <> $id AND d.user_id IS NULL
            RETURN DISTINCT r.id AS id, r.extraction_text AS extraction_text,
                   r.duplicate_group AS duplicate_group
            ORDER BY id
            LIMIT 50
            """,
            key=norm_key,
            id=restriction_id,
            doc_id=doc_id,
        )

    async def set_duplicate_group(self, ids: list[str], group: str | None) -> None:
        await self.client.run(
            """
            UNWIND $ids AS id
            MATCH (r:Restriction {id: id})
            SET r.duplicate_group = $group
            """,
            ids=ids,
            group=group,
        )

    async def restrictions_for_consolidation(self) -> list[dict]:
        """Every restriction with what its kind and duplicate group are computed from."""
        return await self.client.run("""
            MATCH (r:Restriction)
            OPTIONAL MATCH (r)-[:HAS_SUBJECT]->(s:Entity)
            OPTIONAL MATCH (r)-[:APPLIES_TO]->(o:Entity)
            OPTIONAL MATCH (r)-[:DERIVED_FROM]->(:Clause)-[:IN_DOCUMENT]->(d:Document)
            WITH r, s, o, collect(d)[0] AS d
            RETURN r.id AS id, r.kind AS kind, r.kind_label AS kind_label,
                   r.value_operator AS value_operator, r.value_number AS value_number,
                   r.value_unit AS value_unit, r.value_condition AS value_condition,
                   r.measurement_json AS measurement_json,
                   r.extraction_text AS extraction_text,
                   coalesce(s.normalized, r.subject) AS subject,
                   coalesce(o.normalized, r.object) AS object,
                   r.norm_key AS norm_key, r.duplicate_group AS duplicate_group,
                   d IS NOT NULL AND d.user_id IS NULL AS shared
            ORDER BY r.id
            """)

    async def update_restriction_kinds(self, rows: list[dict]) -> None:
        """Set ``[{id, kind, kind_label, kind_status, norm_key, duplicate_group}]``.

        The ``OF_KIND`` edge follows the kind; the listed kinds must exist. The vector of a
        restriction whose kind changes embeds the former kind: it is marked stale (version 0)
        for the re-embedding pass.
        """
        await self.client.run(
            """
            UNWIND $rows AS row
            MATCH (r:Restriction {id: row.id})
            SET r.embedding_version = CASE WHEN coalesce(r.kind, '') <> row.kind
                                           THEN 0 ELSE r.embedding_version END,
                r.kind = row.kind, r.kind_label = row.kind_label,
                r.kind_status = row.kind_status, r.norm_key = row.norm_key,
                r.duplicate_group = row.duplicate_group
            WITH r, row
            OPTIONAL MATCH (r)-[old:OF_KIND]->(previous:RestrictionKind)
            WHERE previous.name <> row.kind
            DELETE old
            WITH DISTINCT r, row
            MATCH (k:RestrictionKind {name: row.kind})
            MERGE (r)-[:OF_KIND]->(k)
            """,
            rows=rows,
        )

    async def remove_unlisted_kinds(self, listed: list[str]) -> int:
        """Delete kinds outside the list that no restriction has any more."""
        rows = await self.client.run(
            """
            MATCH (k:RestrictionKind)
            WHERE NOT k.name IN $listed AND NOT EXISTS { (:Restriction)-[:OF_KIND]->(k) }
            DETACH DELETE k
            RETURN count(*) AS removed
            """,
            listed=listed,
        )
        return rows[0]["removed"] if rows else 0

    async def get_clauses(self, doc_id: str) -> list[dict]:
        """Textual clauses of a document, in reading order (for extraction)."""
        return await self.client.run(
            """
            MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})
            WHERE c.text IS NOT NULL AND c.text <> ''
            RETURN c.node_id AS node_id, c.text AS text,
                   c.char_start AS char_start, c.version_id AS version_id,
                   c.breadcrumb AS breadcrumb, c.numbering AS numbering
            ORDER BY c.order
            """,
            doc_id=doc_id,
        )

    async def clause_contexts(self, doc_id: str) -> dict[str, ClauseContext]:
        """Linked clauses and unresolved references of every clause of a document."""
        return await load_clause_contexts(
            self.client,
            "MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})\n",
            doc_id=doc_id,
        )

    # --- lifecycle: delete / prune / reconcile (stage 5) -----------------------------

    async def documents_by_name(
        self, name: str, *, user_id: str | None = None, scenario_id: str | None = None
    ) -> list[dict]:
        """Stored documents (doc_id + version) sharing a registry name.

        ``user_id``/``scenario_id`` narrow the match to one user document index — required for a
        scoped deletion, since document ``name`` is not unique across users/the shared corpus.
        """
        return await self.client.run(
            """
            MATCH (d:Document {name: $name})
            WHERE ($user_id IS NULL OR d.user_id = $user_id)
              AND ($scenario_id IS NULL OR d.scenario_id = $scenario_id)
            RETURN d.doc_id AS doc_id, d.version AS version,
                   d.version_id AS version_id
            """,
            name=name,
            user_id=user_id,
            scenario_id=scenario_id,
        )

    _STORED_DOCUMENT_FIELDS = """
            RETURN d.doc_id AS doc_id, d.name AS name, d.version AS version,
                   d.version_id AS version_id, d.content_hash AS content_hash,
                   d.extraction_incomplete AS extraction_incomplete,
                   d.extraction_failed_clause_ids AS extraction_failed_clause_ids
            """

    async def stored_documents(self) -> list[dict]:
        """Identity + change-detection fields of every stored document (for reconcile)."""
        return await self.client.run(
            "MATCH (d:Document)" + self._STORED_DOCUMENT_FIELDS
        )

    async def stored_document(self, doc_id: str) -> dict | None:
        """The ``stored_documents`` row of one document, or ``None`` when it is absent."""
        rows = await self.client.run(
            "MATCH (d:Document {doc_id: $doc_id})" + self._STORED_DOCUMENT_FIELDS,
            doc_id=doc_id,
        )
        return rows[0] if rows else None

    # --- pending sync jobs (src/sync/queue.py) ------------------------------------------

    async def save_sync_job(self, props: dict) -> None:
        await self.client.run(
            "MERGE (j:SyncJob {key: $key}) SET j = $props",
            key=props["key"],
            props={k: v for k, v in props.items() if v is not None},
        )

    async def delete_sync_job(self, key: str) -> None:
        await self.client.run("MATCH (j:SyncJob {key: $key}) DELETE j", key=key)

    async def sync_jobs(self) -> list[dict]:
        rows = await self.client.run("MATCH (j:SyncJob) RETURN properties(j) AS job")
        return [row["job"] for row in rows]

    async def documents_without_restrictions(
        self, *, after_id: str | None = None, limit: int = 1
    ) -> list[dict]:
        """Read a keyset page of ingested documents with no extracted restrictions."""
        return await self.client.run(
            """
            MATCH (d:Document)
            WHERE ($after_id IS NULL OR d.doc_id > $after_id)
              AND NOT EXISTS {
                  MATCH (r:Restriction) WHERE r.doc_id = d.doc_id
              }
            RETURN d.doc_id AS doc_id
            ORDER BY d.doc_id
            LIMIT $limit
            """,
            after_id=after_id,
            limit=limit,
        )

    async def document_sync_state(self, doc_id: str) -> dict | None:
        """Change-detection state of a stored document: its ``content_hash`` and how many
        restrictions were already extracted from it. ``None`` when the document is absent.

        Used by the idempotency guard so replaying an already-synced document (event replay,
        reconcile overlap, retry) skips the expensive re-extraction when nothing changed.
        """
        rows = await self.client.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            OPTIONAL MATCH (r:Restriction {doc_id: $doc_id})
            RETURN d.content_hash AS content_hash, count(r) AS restrictions,
                   d.extraction_incomplete AS extraction_incomplete
            """,
            doc_id=doc_id,
        )
        return rows[0] if rows else None

    async def delete_restrictions_of_doc(self, doc_id: str) -> int:
        """Drop every restriction extracted from a document (before a fresh re-extract).

        Restrictions carry no inbound edges from other documents (``SHARES_ENTITY`` is rebuilt
        on extraction), so deleting and re-deriving them keeps a re-extracted document free of
        stale triples without touching the shared entity/kind vocabulary.
        """
        rows = await self.client.run(
            """
            MATCH (r:Restriction {doc_id: $doc_id})
            WITH collect(r) AS rs, count(r) AS deleted
            FOREACH (x IN rs | DETACH DELETE x)
            RETURN deleted
            """,
            doc_id=doc_id,
        )
        return rows[0]["deleted"] if rows else 0

    async def delete_restrictions_of_clauses(
        self, doc_id: str, clause_node_ids: list[str]
    ) -> int:
        """Drop the restrictions extracted from some clauses of a document (before their
        re-extraction is written)."""
        if not clause_node_ids:
            return 0
        rows = await self.client.run(
            """
            MATCH (r:Restriction {doc_id: $doc_id})
            WHERE r.clause_node_id IN $clause_node_ids
            WITH collect(r) AS rs, count(r) AS deleted
            FOREACH (x IN rs | DETACH DELETE x)
            RETURN deleted
            """,
            doc_id=doc_id,
            clause_node_ids=clause_node_ids,
        )
        return rows[0]["deleted"] if rows else 0

    async def prune_clauses(self, doc_id: str, keep_node_ids: list[str]) -> int:
        """Remove clauses of a document that are gone from its current version.

        Deletes each stale ``:Clause`` together with any restrictions derived from it. Surviving
        clauses keep their inbound ``REFERENCES`` edges (so cross-document links are preserved).
        """
        rows = await self.client.run(
            """
            MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})
            WHERE NOT c.node_id IN $keep
            OPTIONAL MATCH (r:Restriction)-[:DERIVED_FROM]->(c)
            WITH collect(DISTINCT c) AS cs, collect(DISTINCT r) AS rs,
                 count(DISTINCT c) AS pruned
            FOREACH (x IN rs | DETACH DELETE x)
            FOREACH (x IN cs | DETACH DELETE x)
            RETURN pruned
            """,
            doc_id=doc_id,
            keep=keep_node_ids,
        )
        return rows[0]["pruned"] if rows else 0

    async def delete_document(self, doc_id: str) -> dict:
        """Delete a document with its clauses and their restrictions.

        The shared vocabulary (``:Entity`` / ``:RestrictionKind``) is left intact; only the
        document-scoped nodes are removed. Returns the counts removed.
        """
        rows = await self.client.run(
            """
            MATCH (d:Document {doc_id: $doc_id})
            OPTIONAL MATCH (c:Clause)-[:IN_DOCUMENT]->(d)
            OPTIONAL MATCH (r:Restriction)-[:DERIVED_FROM]->(c)
            WITH d, collect(DISTINCT c) AS cs, collect(DISTINCT r) AS rs
            WITH d, cs, rs, size(cs) AS clauses, size(rs) AS restrictions
            FOREACH (x IN rs | DETACH DELETE x)
            FOREACH (x IN cs | DETACH DELETE x)
            DETACH DELETE d
            RETURN clauses, restrictions
            """,
            doc_id=doc_id,
        )
        return rows[0] if rows else {"clauses": 0, "restrictions": 0}

    async def delete_scope(self, user_id: str, scenario_id: str) -> dict:
        """Delete every document (+ clauses + restrictions) belonging to one user index.

        Same shape as :meth:`delete_document` but scope-wide — used when a whole user document
        index is wiped (IDU_DVD's ``UserIndexService.delete_index`` does not emit a per-document
        ``DocumentDeleted`` event, so this is driven by an explicit admin call, not the consumer).
        The shared vocabulary (``:Entity`` / ``:RestrictionKind``) is left intact.
        """
        rows = await self.client.run(
            """
            MATCH (d:Document {user_id: $user_id, scenario_id: $scenario_id})
            OPTIONAL MATCH (c:Clause)-[:IN_DOCUMENT]->(d)
            OPTIONAL MATCH (r:Restriction)-[:DERIVED_FROM]->(c)
            WITH collect(DISTINCT d) AS ds, collect(DISTINCT c) AS cs, collect(DISTINCT r) AS rs
            WITH ds, cs, rs, size(ds) AS documents, size(cs) AS clauses, size(rs) AS restrictions
            FOREACH (x IN rs | DETACH DELETE x)
            FOREACH (x IN cs | DETACH DELETE x)
            FOREACH (x IN ds | DETACH DELETE x)
            RETURN documents, clauses, restrictions
            """,
            user_id=user_id,
            scenario_id=scenario_id,
        )
        return rows[0] if rows else {"documents": 0, "clauses": 0, "restrictions": 0}

    async def stats(self) -> dict:
        """Node/edge counts for a quick health/coverage view."""
        rows = await self.client.run("""
            CALL () { MATCH (d:Document) RETURN count(d) AS documents }
            CALL () { MATCH (c:Clause) RETURN count(c) AS clauses }
            CALL () { MATCH (:Clause)-[r:REFERENCES]->() RETURN count(r) AS references }
            CALL () { MATCH (p:PendingReference) RETURN count(p) AS pending_references }
            CALL () { MATCH (rr:Restriction) RETURN count(rr) AS restrictions }
            RETURN documents, clauses, references, pending_references, restrictions
            """)
        return rows[0] if rows else {}

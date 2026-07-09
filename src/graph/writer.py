"""Write helpers for the ingestion path.

All writes are idempotent (``MERGE`` on the natural key + ``SET += props``), so re-ingesting a
document or ingesting documents out of order converges to the same graph. A referenced clause that
is not in the store yet is ``MERGE``-d as a thin ``:Clause`` node and filled in when its own
document is later ingested; an unresolved reference lands on a ``:PendingReference`` stub.
"""

from __future__ import annotations

import structlog

from src.dvd_client.models import DocumentRef
from src.graph.client import Neo4jClient

log = structlog.get_logger(__name__)


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().split())


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

    async def get_kind(self, name: str) -> dict | None:
        """Exact kind lookup by canonical name or alias."""
        rows = await self.client.run(
            """
            MATCH (k:RestrictionKind)
            WHERE k.name = $name OR $name IN coalesce(k.aliases, [])
            RETURN k.name AS name, k.status AS status
            LIMIT 1
            """,
            name=name,
        )
        return rows[0] if rows else None

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
            """,
            id=props["id"],
            props=props,
            embedding=embedding,
            clause=clause_node_id,
            subject=subject_normalized,
            object=object_normalized,
            kind=kind_name,
        )

    async def link_shares_entity(self, restriction_id: str) -> int:
        """Connect a restriction to every other restriction sharing a subject/object entity."""
        rows = await self.client.run(
            """
            MATCH (r:Restriction {id: $id})-[:HAS_SUBJECT|APPLIES_TO]->(e:Entity)
                  <-[:HAS_SUBJECT|APPLIES_TO]-(other:Restriction)
            WHERE other.id <> r.id
            MERGE (r)-[:SHARES_ENTITY]-(other)
            RETURN count(DISTINCT other) AS linked
            """,
            id=restriction_id,
        )
        return rows[0]["linked"] if rows else 0

    async def get_clauses(self, doc_id: str) -> list[dict]:
        """Textual clauses of a document, in reading order (for extraction)."""
        return await self.client.run(
            """
            MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})
            WHERE c.text IS NOT NULL AND c.text <> ''
            RETURN c.node_id AS node_id, c.text AS text,
                   c.char_start AS char_start, c.version_id AS version_id
            ORDER BY c.order
            """,
            doc_id=doc_id,
        )

    # --- lifecycle: delete / prune / reconcile (stage 5) -----------------------------

    async def documents_by_name(self, name: str) -> list[dict]:
        """Stored documents (doc_id + version) sharing a registry name."""
        return await self.client.run(
            """
            MATCH (d:Document {name: $name})
            RETURN d.doc_id AS doc_id, d.version AS version,
                   d.version_id AS version_id
            """,
            name=name,
        )

    async def stored_documents(self) -> list[dict]:
        """Identity + change-detection fields of every stored document (for reconcile)."""
        return await self.client.run("""
            MATCH (d:Document)
            RETURN d.doc_id AS doc_id, d.name AS name, d.version AS version,
                   d.version_id AS version_id, d.content_hash AS content_hash
            """)

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

"""Bounded read-only projections for the admin UI; vectors never leave the server."""

from src.graph.client import Neo4jClient

_COUNTS = """
WITH d,
     COUNT { MATCH (c:Clause)-[:IN_DOCUMENT]->(d) } AS clauses,
     COUNT { MATCH (r:Restriction) WHERE r.doc_id = d.doc_id } AS restrictions
WITH d, clauses, restrictions,
     CASE
       WHEN d.extraction_incomplete = true THEN 'incomplete'
       WHEN d.extraction_incomplete = false THEN 'complete'
       WHEN clauses = 0 THEN 'no_clauses'
       ELSE 'unknown'
     END AS state
"""
_DOCUMENT = """
d { .doc_id, .name, .title, .version, .version_id, .corpus, .doc_type,
    .lang, .uploaded_at, .content_hash, .user_id, .scenario_id,
    .extraction_incomplete, .extraction_failed_clause_ids,
    clauses: clauses, restrictions: restrictions, state: state } AS document
"""


def page(rows: list[dict], limit: int, key: str) -> dict:
    items = rows[:limit]
    more = len(rows) > limit
    return {
        "items": items,
        "has_more": more,
        "next_after": items[-1][key] if more else None,
    }


class AdminRepository:
    def __init__(self, graph: Neo4jClient):
        self.graph = graph

    async def reprocessing_documents(self) -> list[dict]:
        """Loaded documents only; exclude reference placeholders without clauses."""
        return await self.graph.run("""
            MATCH (d:Document)
            WHERE EXISTS { MATCH (:Clause)-[:IN_DOCUMENT]->(d) }
            RETURN d.doc_id AS doc_id, d.name AS name
            ORDER BY d.doc_id
            """)

    async def documents(
        self, query: str = "", state: str = "", after: str = "", limit: int = 50
    ) -> dict:
        # Without a state filter, count only the requested page, not the entire corpus.
        candidates = "" if state else "WITH d ORDER BY d.doc_id LIMIT $limit\n"
        rows = await self.graph.run(
            """
            MATCH (d:Document)
            WHERE d.doc_id > $after
              AND ($search = '' OR toLower(coalesce(d.name, '')) CONTAINS $search
                   OR toLower(d.doc_id) CONTAINS $search)
            """
            + candidates
            + _COUNTS
            + "WHERE $state = '' OR state = $state\nRETURN "
            + _DOCUMENT
            + " ORDER BY d.doc_id LIMIT $limit",
            search=query.lower(),
            state=state,
            after=after,
            limit=limit + 1,
        )
        return page([r["document"] for r in rows], limit, "doc_id")

    async def document(self, doc_id: str) -> dict | None:
        rows = await self.graph.run(
            "MATCH (d:Document {doc_id: $doc_id})\n" + _COUNTS + "RETURN " + _DOCUMENT,
            doc_id=doc_id,
        )
        return rows[0]["document"] if rows else None

    async def clauses(self, doc_id: str, after: str = "", limit: int = 50) -> dict:
        rows = await self.graph.run(
            """
            MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})
            WHERE c.node_id > $after
            RETURN c { .node_id, .numbering, .breadcrumb, .text, .kind, .tags,
                       .char_start, .char_end } AS clause
            ORDER BY c.node_id LIMIT $limit
            """,
            doc_id=doc_id,
            after=after,
            limit=limit + 1,
        )
        return page([r["clause"] for r in rows], limit, "node_id")

    async def restrictions(self, doc_id: str, after: str = "", limit: int = 50) -> dict:
        rows = await self.graph.run(
            """
            MATCH (r:Restriction {doc_id: $doc_id})
            WHERE r.id > $after
            WITH r ORDER BY r.id LIMIT $limit
            OPTIONAL MATCH (r)-[:DERIVED_FROM]->(c:Clause)
            RETURN r { .id, .subject, .object, .kind, .extraction_text,
                       .value_operator, .value_number, .value_unit, .value_condition,
                       numbering: c.numbering, breadcrumb: c.breadcrumb } AS restriction
            ORDER BY r.id
            """,
            doc_id=doc_id,
            after=after,
            limit=limit + 1,
        )
        return page([r["restriction"] for r in rows], limit, "id")

"""Read side of the restriction graph: vector search, filtered listing, traversal, facets.

All restriction reads share one projection (``_RETURN``) and one filter predicate (``_WHERE``) so
search, applicable and get-by-id return identically shaped rows. Vector search over-fetches from the
native index and then applies the document/clause/entity filters, since the filters live on
neighbours of the restriction node, not on the node itself.
"""

from __future__ import annotations

from src.graph.client import Neo4jClient

# Joins from a restriction to its clause, document, entities and kind.
_MATCH = """
MATCH (r)-[:DERIVED_FROM]->(c:Clause)-[:IN_DOCUMENT]->(d:Document)
MATCH (r)-[:HAS_SUBJECT]->(subj:Entity)
MATCH (r)-[:APPLIES_TO]->(obj:Entity)
MATCH (r)-[:OF_KIND]->(k:RestrictionKind)
OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]->(cp:CheckPlan {current: true})
"""

# Every filter is null-guarded so a single query serves any combination.
_WHERE = """
WHERE ($kind IS NULL OR k.name = $kind)
  AND ($doc_id IS NULL OR d.doc_id = $doc_id)
  AND ($document_names IS NULL OR d.name IN $document_names)
  AND ($doc_type IS NULL OR d.doc_type = $doc_type)
  AND ($corpus IS NULL OR d.corpus = $corpus)
  AND ($lang IS NULL OR d.lang = $lang)
  AND ($version IS NULL OR c.version = $version OR c.version_id = $version)
  AND ($tags IS NULL OR any(t IN $tags WHERE t IN coalesce(c.tags, [])))
  AND ($subject IS NULL OR subj.normalized = $subject
       OR $subject IN coalesce(subj.aliases, []))
  AND ($object IS NULL OR obj.normalized = $object
       OR $object IN coalesce(obj.aliases, []))
"""

_RETURN = """
RETURN r.id AS id, r.subject AS subject, r.object AS object, r.kind AS kind,
       r.kind_status AS kind_status, r.extraction_text AS extraction_text,
       r.value_operator AS value_operator, r.value_number AS value_number,
       r.value_unit AS value_unit, r.value_condition AS value_condition,
       {score} AS score,
       subj.normalized AS subject_normalized, obj.normalized AS object_normalized,
       c.node_id AS clause_node_id, c.numbering AS numbering,
       c.breadcrumb AS breadcrumb, c.tags AS tags,
       c.char_start AS char_start, c.char_end AS char_end,
       d.doc_id AS doc_id, d.name AS name, d.version AS version,
       d.version_id AS version_id, d.doc_type AS doc_type,
       d.corpus AS corpus, d.lang AS lang,
       cp.schema_version AS check_schema_version,
       cp.template AS check_template,
       cp.template_version AS check_template_version,
       cp.params_json AS check_params_json,
       cp.requirements_json AS check_requirements_json,
       cp.source_json AS check_source_json,
       cp.planner_status AS check_planner_status,
       cp.review_status AS check_review_status,
       cp.revision AS check_revision
"""

# Default keys so a partial filter dict still binds every Cypher parameter.
_FILTER_KEYS = (
    "kind",
    "doc_id",
    "document_names",
    "version",
    "doc_type",
    "corpus",
    "lang",
    "tags",
    "subject",
    "object",
)


def _filter_params(filters: dict) -> dict:
    return {key: filters.get(key) for key in _FILTER_KEYS}


class GraphReader:
    def __init__(self, client: Neo4jClient) -> None:
        self.client = client

    async def search_vector(
        self,
        index: str,
        embedding: list[float],
        filters: dict,
        *,
        limit: int,
        oversample: int = 5,
    ) -> list[dict]:
        k = max(limit * oversample, 50)
        query = (
            "CALL db.index.vector.queryNodes($index, $k, $vec) YIELD node AS r, score\n"
            + _MATCH
            + _WHERE
            + _RETURN.format(score="score")
            + "\nORDER BY score DESC\nLIMIT $limit"
        )
        params = {"index": index, "k": k, "vec": embedding, "limit": limit}
        params.update(_filter_params(filters))
        return await self.client.run(query, **params)

    async def search_filter(self, filters: dict, *, limit: int) -> list[dict]:
        query = (
            "MATCH (r:Restriction)\n"
            + _MATCH
            + _WHERE
            + _RETURN.format(score="null")
            + "\nORDER BY d.name, c.numbering\nLIMIT $limit"
        )
        params = {"limit": limit}
        params.update(_filter_params(filters))
        return await self.client.run(query, **params)

    async def get_by_ids(self, ids: list[str]) -> list[dict]:
        query = (
            "MATCH (r:Restriction) WHERE r.id IN $ids\n"
            + _MATCH
            + _RETURN.format(score="null")
        )
        return await self.client.run(query, ids=ids)

    async def applicable(
        self, object_normalized: list[str], filters: dict, *, limit: int
    ) -> list[dict]:
        query = (
            "MATCH (r:Restriction)-[:APPLIES_TO]->(target:Entity)\n"
            "WHERE target.normalized IN $targets\n"
            + _MATCH
            + _WHERE
            + _RETURN.format(score="null")
            + "\nORDER BY d.name, c.numbering\nLIMIT $limit"
        )
        params = {"targets": object_normalized, "limit": limit}
        params.update(_filter_params(filters))
        return await self.client.run(query, **params)

    async def neighbors(self, ids: list[str]) -> list[dict]:
        """One-hop related restrictions: shared entity, or a document cross-reference."""
        return await self.client.run(
            """
            MATCH (r:Restriction) WHERE r.id IN $ids
            CALL {
              WITH r
              MATCH (r)-[:SHARES_ENTITY]-(n:Restriction)
              RETURN n, 'shares_entity' AS relation
              UNION
              WITH r
              MATCH (r)-[:DERIVED_FROM]->(:Clause)-[:REFERENCES]-(:Clause)
                    <-[:DERIVED_FROM]-(n:Restriction)
              RETURN n, 'reference' AS relation
            }
            WITH r.id AS src, n.id AS neighbor_id, relation
            WHERE neighbor_id <> src
            RETURN DISTINCT src, neighbor_id, relation
            """,
            ids=ids,
        )

    async def conflict_pairs(
        self,
        *,
        user_id: str | None = None,
        scenario_id: str | None = None,
        restriction_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Raw ``CONFLICTS_WITH`` edges (id pairs + reason/severity), each pair returned once.

        With ``restriction_id``, only that restriction's conflicts. With ``user_id``/
        ``scenario_id`` (no ``restriction_id``), every pair where at least one side belongs to
        that user index — covers both self-consistency conflicts (both sides in scope) and
        corpus-vs-user conflicts (one side in scope, the other the shared corpus or another user).
        With neither, conflicts graph-wide. Caller resolves ids to full rows via ``get_by_ids``.
        """
        return await self.client.run(
            """
            MATCH (r:Restriction)-[c:CONFLICTS_WITH]-(o:Restriction)
            WHERE r.id < o.id
              AND ($restriction_id IS NULL OR r.id = $restriction_id OR o.id = $restriction_id)
              AND ($user_id IS NULL OR
                   EXISTS {
                     MATCH (r)-[:DERIVED_FROM]->(:Clause)-[:IN_DOCUMENT]->(rd:Document)
                     WHERE rd.user_id = $user_id AND rd.scenario_id = $scenario_id
                   } OR
                   EXISTS {
                     MATCH (o)-[:DERIVED_FROM]->(:Clause)-[:IN_DOCUMENT]->(od:Document)
                     WHERE od.user_id = $user_id AND od.scenario_id = $scenario_id
                   })
            RETURN r.id AS restriction_id, o.id AS other_id,
                   c.reason AS reason, c.severity AS severity
            LIMIT $limit
            """,
            user_id=user_id,
            scenario_id=scenario_id,
            restriction_id=restriction_id,
            limit=limit,
        )

    async def nearest_entities(
        self, index: str, embedding: list[float], *, k: int = 5
    ) -> list[dict]:
        """Read-only nearest-entity lookup (for applicable-restriction resolution)."""
        return await self.client.run(
            """
            CALL db.index.vector.queryNodes($index, $k, $vec)
            YIELD node, score
            RETURN node.normalized AS normalized, score
            """,
            index=index,
            k=k,
            vec=embedding,
        )

    async def list_entities(self, query: str | None, *, limit: int) -> list[dict]:
        return await self.client.run(
            """
            MATCH (e:Entity)
            WHERE $q IS NULL OR toLower(e.normalized) CONTAINS toLower($q)
            OPTIONAL MATCH (:Restriction)-[:HAS_SUBJECT|APPLIES_TO]->(e)
            WITH e, count(*) AS restriction_count
            RETURN e.normalized AS normalized, e.name AS name,
                   coalesce(e.aliases, []) AS aliases,
                   coalesce(e.status, 'active') AS status, restriction_count
            ORDER BY restriction_count DESC, e.normalized
            LIMIT $limit
            """,
            q=query,
            limit=limit,
        )

    async def list_kinds(self) -> list[dict]:
        return await self.client.run("""
            MATCH (k:RestrictionKind)
            OPTIONAL MATCH (:Restriction)-[:OF_KIND]->(k)
            WITH k, count(*) AS restriction_count
            RETURN k.name AS name, coalesce(k.status, 'approved') AS status,
                   coalesce(k.aliases, []) AS aliases, restriction_count
            ORDER BY restriction_count DESC, k.name
            """)

    async def check_plan_revisions(self, restriction_id: str) -> list[dict]:
        return await self.client.run(
            """
            MATCH (:Restriction {id: $restriction_id})-[:HAS_CHECK_PLAN]->(cp:CheckPlan)
            RETURN cp.restriction_id AS restriction_id,
                   cp.schema_version AS schema_version,
                   cp.template AS template,
                   cp.template_version AS template_version,
                   cp.params_json AS params_json,
                   cp.requirements_json AS requirements_json,
                   cp.source_json AS source_json,
                   cp.planner_status AS planner_status,
                   cp.review_status AS review_status,
                   cp.revision AS revision,
                   cp.author AS author,
                   cp.reason AS reason,
                   toString(cp.created_at) AS created_at,
                   cp.current AS current
            ORDER BY cp.revision DESC
            """,
            restriction_id=restriction_id,
        )

    async def pending_check_plans(self, limit: int = 100) -> list[dict]:
        return await self.client.run(
            """
            MATCH (:Restriction)-[:HAS_CHECK_PLAN]->(cp:CheckPlan {
                current: true, planner_status: 'auto'
            })
            RETURN cp.restriction_id AS restriction_id,
                   cp.schema_version AS schema_version,
                   cp.template AS template,
                   cp.template_version AS template_version,
                   cp.params_json AS params_json,
                   cp.requirements_json AS requirements_json,
                   cp.source_json AS source_json,
                   cp.planner_status AS planner_status,
                   cp.review_status AS review_status,
                   cp.revision AS revision,
                   cp.author AS author,
                   cp.reason AS reason,
                   toString(cp.created_at) AS created_at,
                   cp.current AS current
            ORDER BY cp.created_at
            LIMIT $limit
            """,
            limit=limit,
        )

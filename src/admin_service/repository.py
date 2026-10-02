"""Bounded read-only projections for the admin UI; vectors never leave the server."""

import json

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


# Restrictions matching the admin filters, joined to their current plan. The plan filter
# follows a WITH: a WHERE right after OPTIONAL MATCH would only make the plan optional.
_FILTERED_RESTRICTIONS = """
MATCH (r:Restriction)
WHERE r.id > $after
  AND ($doc_id = '' OR r.doc_id = $doc_id)
  AND ($kind = '' OR r.kind = $kind)
  AND ($search = '' OR r.id = $search_raw
       OR toLower(coalesce(r.subject, '')) CONTAINS $search
       OR toLower(coalesce(r.object, '')) CONTAINS $search
       OR toLower(coalesce(r.extraction_text, '')) CONTAINS $search)
OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]->(cp:CheckPlan {current: true})
WITH r, cp
WHERE ($plan = ''
       OR ($plan = 'none' AND cp IS NULL)
       OR ($plan = 'executable' AND cp.planner_status IN ['auto', 'reviewed'])
       OR cp.planner_status = $plan)
  AND ($template = '' OR cp.template = $template)
"""


def _json(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value  # shown as stored rather than hidden


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

    async def restriction_search(
        self,
        *,
        query: str = "",
        doc_id: str = "",
        kind: str = "",
        plan: str = "",
        template: str = "",
        after: str = "",
        limit: int = 50,
    ) -> dict:
        """A keyset page of restrictions across documents; the first page also counts them."""
        filters = {
            "search": query.strip().lower(),
            "search_raw": query.strip(),
            "doc_id": doc_id,
            "kind": kind,
            "plan": plan,
            "template": template,
        }
        rows = await self.graph.run(
            _FILTERED_RESTRICTIONS + """
            WITH r, cp ORDER BY r.id LIMIT $limit
            OPTIONAL MATCH (r)-[:DERIVED_FROM]->(c:Clause)
            OPTIONAL MATCH (d:Document {doc_id: r.doc_id})
            RETURN r { .id, .subject, .object, .kind, .extraction_text, .doc_id,
                       .value_operator, .value_number, .value_unit, .value_condition,
                       numbering: c.numbering, document_name: d.name,
                       plan_status: cp.planner_status, plan_template: cp.template,
                       plan_review_status: cp.review_status } AS restriction
            ORDER BY r.id
            """,
            after=after,
            limit=limit + 1,
            **filters,
        )
        result = page([r["restriction"] for r in rows], limit, "id")
        result["total"] = None
        if not after:
            counted = await self.graph.run(
                _FILTERED_RESTRICTIONS + "RETURN count(*) AS total",
                after="",
                **filters,
            )
            result["total"] = counted[0]["total"] if counted else 0
        return result

    async def restriction_facets(self) -> dict:
        """Values the restriction filters offer, each with its number of restrictions."""
        rows = await self.graph.run("""
            MATCH (r:Restriction)
            OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]->(cp:CheckPlan {current: true})
            RETURN r.doc_id AS doc_id, r.kind AS kind,
                   coalesce(cp.planner_status, 'none') AS plan,
                   cp.template AS template, count(*) AS restrictions
            """)
        facets: dict[str, dict] = {
            "documents": {},
            "kinds": {},
            "plans": {},
            "templates": {},
        }
        for row in rows:
            for facet, key in (
                ("documents", row["doc_id"]),
                ("kinds", row["kind"]),
                ("plans", row["plan"]),
                ("templates", row["template"]),
            ):
                if key is not None:
                    counts = facets[facet]
                    counts[key] = counts.get(key, 0) + row["restrictions"]
        names = {}
        if facets["documents"]:
            documents = await self.graph.run(
                """
                MATCH (d:Document) WHERE d.doc_id IN $ids
                RETURN d.doc_id AS doc_id, d.name AS name
                """,
                ids=list(facets["documents"]),
            )
            names = {row["doc_id"]: row["name"] for row in documents}

        def ordered(counts: dict) -> list[dict]:
            return [
                {"value": value, "restrictions": count}
                for value, count in sorted(counts.items(), key=lambda item: item[0])
            ]

        return {
            "documents": sorted(
                (
                    {
                        "doc_id": doc_id,
                        "name": names.get(doc_id),
                        "restrictions": count,
                    }
                    for doc_id, count in facets["documents"].items()
                ),
                key=lambda item: ((item["name"] or item["doc_id"]).lower()),
            ),
            "kinds": ordered(facets["kinds"]),
            "plans": ordered(facets["plans"]),
            "templates": ordered(facets["templates"]),
        }

    async def restriction(self, restriction_id: str) -> dict | None:
        """One restriction with its clause, document and current check plan."""
        rows = await self.graph.run(
            """
            MATCH (r:Restriction {id: $id})
            OPTIONAL MATCH (r)-[:DERIVED_FROM]->(c:Clause)
            OPTIONAL MATCH (d:Document {doc_id: r.doc_id})
            OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]->(cp:CheckPlan {current: true})
            RETURN r { .id, .subject, .object, .kind, .kind_status, .extraction_text,
                       .value_operator, .value_number, .value_unit, .value_condition,
                       .measurement_json, .doc_id } AS restriction,
                   c { .node_id, .numbering, .breadcrumb, .text } AS clause,
                   d { .doc_id, .name, .version, .corpus, .user_id,
                       .scenario_id } AS document,
                   cp { .revision, .template, .template_version, .params_json,
                        .requirements_json, .source_json, .planner_status,
                        .review_status, .author, .reason, .planner_version,
                        created_at: toString(cp.created_at) } AS plan,
                   COUNT { (r)-[:HAS_CHECK_PLAN]->(:CheckPlan) } AS plan_revisions
            LIMIT 1
            """,
            id=restriction_id,
        )
        if not rows:
            return None
        row = rows[0]
        restriction = dict(row["restriction"])
        restriction["measurement"] = _json(restriction.pop("measurement_json", None))
        plan = row["plan"]
        if plan is not None:
            plan = dict(plan)
            for field, name in (
                ("params_json", "params"),
                ("requirements_json", "declared_requirements"),
                ("source_json", "source"),
            ):
                plan[name] = _json(plan.pop(field, None))
        return {
            "restriction": restriction,
            "clause": row["clause"],
            "document": row["document"],
            "plan": plan,
            "plan_revisions": row["plan_revisions"],
        }

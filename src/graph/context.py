"""Read the clauses a clause points to (see ``src/pipeline/clause_context.py``)."""

from __future__ import annotations

from src.graph.client import Neo4jClient
from src.pipeline.clause_context import MIN_RELATION_WEIGHT, ClauseContext

# For a bound ``c:Clause``: ``depends`` — DVD relations needed to apply it; ``references`` —
# each REFERENCES edge with its target clause when one is known. A reference to a document,
# or a pending one whose name starts a stored document's name, is resolved to that
# document's clause with the referenced number.
CLAUSE_CONTEXT = """
CALL (c) {
  OPTIONAL MATCH (c)-[dep:DEPENDS_ON]->(dt:Clause)
  WHERE dep.weight >= $context_min_weight AND dep.kind <> 'same_topic'
    AND coalesce(dt.text, '') <> ''
  RETURN collect({node_id: dt.node_id, numbering: dt.numbering, text: dt.text,
                  relation: dep.kind, weight: dep.weight}) AS depends
}
CALL (c) {
  OPTIONAL MATCH (c)-[ref:REFERENCES]->(t)
  WITH ref, t WHERE t IS NOT NULL
  WITH ref, t,
       coalesce(nullif(ref.target_numbering, ''), t.target_numbering, '') AS numbering
  OPTIONAL MATCH (doc:Document)
  WHERE (t:Document AND doc = t)
     OR (t:PendingReference AND size(coalesce(t.target_name, '')) >= 6
         AND t.target_name =~ '.*[0-9].*'
         AND toLower(doc.name) STARTS WITH toLower(t.target_name))
  OPTIONAL MATCH (tc:Clause)-[:IN_DOCUMENT]->(doc)
  WHERE numbering <> '' AND tc.numbering = numbering AND coalesce(tc.text, '') <> ''
  WITH ref, t, numbering, doc, CASE WHEN t:Clause THEN t ELSE tc END AS target
  WITH ref, t, numbering, collect(target)[0] AS target, collect(doc.name)[0] AS doc_name
  RETURN collect({
    raw: ref.raw,
    target_name: coalesce(t.target_name, doc_name, ''),
    target_numbering: numbering,
    node_id: target.node_id, numbering: target.numbering, text: target.text,
    document: coalesce(target.name, doc_name),
    external: target IS NOT NULL AND coalesce(target.doc_id, '') <> coalesce(c.doc_id, ''),
    in_corpus: doc_name IS NOT NULL
  }) AS references
}
"""


# IDU_DVD keeps a table as its caption («Таблица 6.1 …») and, a few fragments later (after
# amendment notes), the body: a reference to the caption needs the body's rows.
TABLE_BODIES = """
UNWIND $ids AS id
MATCH (t:Clause {node_id: id})-[:IN_DOCUMENT]->(:Document)<-[:IN_DOCUMENT]-(b:Clause)
WHERE b.kind = 'table' AND b.order > t.order AND b.order <= t.order + 4
WITH t, b ORDER BY b.order
RETURN t.node_id AS node_id, collect(b.text)[0] AS body
"""


def _table_caption(text: str | None) -> bool:
    return bool(text) and len(text) < 400 and text.lstrip().startswith("Таблица")


async def load_clause_contexts(
    client: Neo4jClient, match: str, **params
) -> dict[str, ClauseContext]:
    """Contexts of the clauses bound to ``c`` by ``match``, keyed by clause node id."""
    rows = await client.run(
        match + CLAUSE_CONTEXT + "RETURN c.node_id AS node_id, depends, references",
        context_min_weight=MIN_RELATION_WEIGHT,
        **params,
    )
    linked = [
        item
        for row in rows
        for item in (*row["depends"], *row["references"])
        if item.get("node_id") and _table_caption(item.get("text"))
    ]
    if linked:
        bodies = {
            body["node_id"]: body["body"]
            for body in await client.run(
                TABLE_BODIES, ids=sorted({item["node_id"] for item in linked})
            )
            if body["body"]
        }
        for item in linked:
            if item["node_id"] in bodies:
                item["text"] = f"{item['text'].rstrip()}\n{bodies[item['node_id']]}"
    return {
        row["node_id"]: ClauseContext.from_row(
            row["depends"], row["references"], own_node_id=row["node_id"]
        )
        for row in rows
    }

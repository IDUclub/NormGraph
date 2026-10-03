"""The clause-context query against a disposable Neo4j; never use application data."""

import os
import uuid

import pytest

from src.admin_service.repository import AdminRepository
from src.graph.client import Neo4jClient
from src.graph.context import load_clause_contexts

pytestmark = pytest.mark.integration


async def test_links_references_and_pending_references_become_clause_context():
    uri = os.environ.get("NG_ADMIN_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set NG_ADMIN_TEST_NEO4J_URI to a disposable Neo4j")
    graph = Neo4jClient(uri, "neo4j", os.environ["NG_ADMIN_TEST_NEO4J_PASSWORD"])
    p = "ctx-test-" + uuid.uuid4().hex
    try:
        await graph.run(
            """
            CREATE (d:Document {doc_id: $p + '-d', name: $p + ' РНГП'}),
                   (sp:Document {doc_id: $p + '-sp', name: 'СП 9' + $p + '.2016 Планировка'})
            CREATE (c:Clause {node_id: $p + '-c', doc_id: $p + '-d', numbering: '5.3',
                              text: 'по таблице 6.1 и п. 7.1 СП 9' + $p})-[:IN_DOCUMENT]->(d),
                   (t:Clause {node_id: $p + '-t', doc_id: $p + '-d', numbering: '', order: 10,
                              text: 'Таблица 6.1 …', name: $p + ' РНГП'})-[:IN_DOCUMENT]->(d),
                   (:Clause {node_id: $p + '-tn', doc_id: $p + '-d', order: 11, kind: 'text',
                             text: '(в ред. постановления от 01.01.2024 N 1)'})-[:IN_DOCUMENT]->(d),
                   (:Clause {node_id: $p + '-tb', doc_id: $p + '-d', order: 12, kind: 'table',
                             text: 'АЗС | 50 м'})-[:IN_DOCUMENT]->(d),
                   (l:Clause {node_id: $p + '-l', doc_id: $p + '-d', text: 'Лид'})-[:IN_DOCUMENT]->(d),
                   (s:Clause {node_id: $p + '-s', doc_id: $p + '-d', text: 'Тема'})-[:IN_DOCUMENT]->(d),
                   (x:Clause {node_id: $p + '-x', doc_id: $p + '-sp', numbering: '7.1',
                              text: 'не менее 10 м', name: 'СП 9' + $p + '.2016 Планировка'})
                     -[:IN_DOCUMENT]->(sp),
                   (stub:Clause {node_id: $p + '-stub'})
            CREATE (c)-[:REFERENCES {raw: 'таблице 6.1', resolved: true}]->(t),
                   (c)-[:REFERENCES {raw: 'п. 7.1 СП 9', resolved: false}]->
                     (:PendingReference {key: $p + '-p1', target_name: 'СП 9' + $p,
                                         target_numbering: '7.1'}),
                   (c)-[:REFERENCES {raw: 'СанПиН 1', resolved: false}]->
                     (:PendingReference {key: $p + '-p2', target_name: 'СанПиН 1' + $p}),
                   (c)-[:REFERENCES {raw: 'п. 9', resolved: true}]->(stub),
                   (c)-[:DEPENDS_ON {weight: 1.0, kind: 'completes'}]->(l),
                   (c)-[:DEPENDS_ON {weight: 0.3, kind: 'same_topic'}]->(s)
            """,
            p=p,
        )
        contexts = await load_clause_contexts(
            graph,
            "MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id: $doc_id})\n",
            doc_id=p + "-d",
        )
        context = contexts[p + "-c"]
        related = {item.node_id: item for item in context.related}
        assert set(related) == {p + "-t", p + "-x", p + "-l"}
        assert (
            related[p + "-t"].relation == "reference" and not related[p + "-t"].document
        )
        # A table caption brings the table body that follows it.
        assert related[p + "-t"].text == "Таблица 6.1 …\nАЗС | 50 м"
        # The pending reference resolves by the stored document's name and clause number.
        assert related[p + "-x"].document == "СП 9" + p + ".2016 Планировка"
        assert related[p + "-l"].relation == "completes"
        assert sorted(item.raw for item in context.unresolved) == ["СанПиН 1", "п. 9"]
        assert not contexts[p + "-s"]

        await graph.run(
            """
            MATCH (c:Clause {node_id: $p + '-c'})
            CREATE (:Restriction {id: $p + '-r1', doc_id: $p + '-d', subject: $p,
                                  value_source_json: '{"node_id": "t"}'})-[:DERIVED_FROM]->(c),
                   (:Restriction {id: $p + '-r2', doc_id: $p + '-d', subject: $p,
                                  unresolved_references: ['СанПиН 1' + $p]})-[:DERIVED_FROM]->(c)
            """,
            p=p,
        )
        repo = AdminRepository(graph)
        linked = await repo.restriction_search(query=p, reference="linked")
        assert [r["id"] for r in linked["items"]] == [p + "-r1"] and linked[
            "total"
        ] == 1
        assert linked["items"][0]["value_source"] == {"node_id": "t"}
        unresolved = await repo.restriction_search(query=p, reference="unresolved")
        assert [r["id"] for r in unresolved["items"]] == [p + "-r2"]
        facets = await repo.restriction_facets()
        assert facets["references"]["linked"] >= 1
        assert {"reference": "СанПиН 1" + p, "restrictions": 1} in (
            await repo.unresolved_references(100)
        )
        card = await repo.restriction(p + "-r1")
        assert {item["node_id"] for item in card["related"]} == set(related)
    finally:
        await graph.run(
            """
            MATCH (n) WHERE n.doc_id STARTS WITH $p OR n.node_id STARTS WITH $p
                         OR n.key STARTS WITH $p
            DETACH DELETE n
            """,
            p=p,
        )
        await graph.close()

"""Duplicate groups and kind consolidation against a disposable Neo4j; never application data."""

import os
import uuid

import pytest

from src.admin_service.repository import AdminRepository
from src.graph.client import Neo4jClient
from src.graph.reader import GraphReader
from src.graph.writer import GraphWriter
from src.pipeline.kind_consolidation import consolidate
from src.pipeline.vocabulary import KindVocabulary

pytestmark = pytest.mark.integration

LAMPS = "Неисправные люминесцентные лампы хранятся в отдельном помещении"


class _Embedder:
    async def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


async def test_consolidation_groups_duplicates_of_the_shared_corpus_only():
    uri = os.environ.get("NG_ADMIN_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set NG_ADMIN_TEST_NEO4J_URI to a disposable Neo4j")
    graph = Neo4jClient(uri, "neo4j", os.environ["NG_ADMIN_TEST_NEO4J_PASSWORD"])
    p = "dup-test-" + uuid.uuid4().hex
    try:
        await graph.run(
            """
            CREATE (a:Document {doc_id: $p + '-a', name: $p + ' СанПиН А'}),
                   (b:Document {doc_id: $p + '-b', name: $p + ' СанПиН Б'}),
                   (u:Document {doc_id: $p + '-u', name: $p + ' мой', user_id: 'u1',
                                scenario_id: 's1'}),
                   (s:Entity {normalized: $p + ' организация'}),
                   (o:Entity {normalized: $p + ' лампы'}),
                   (old:RestrictionKind {name: 'требование_хранения_' + $p,
                                         status: 'pending'})
            WITH *
            UNWIND [['r1', 'a', '2.8.10', $lamps], ['r2', 'b', '10', $lamps + '.'],
                    ['r3', 'u', '1', $lamps], ['r4', 'a', '3.1', 'мебель с документами']]
                   AS row
            MATCH (d:Document {doc_id: $p + '-' + row[1]})
            CREATE (c:Clause {node_id: $p + '-c-' + row[0], doc_id: d.doc_id,
                              numbering: row[2]})-[:IN_DOCUMENT]->(d)
            CREATE (r:Restriction {id: $p + '-' + row[0], doc_id: d.doc_id,
                                   subject: $p + ' Организация', object: 'Лампы',
                                   kind: 'требование_хранения_' + $p,
                                   extraction_text: row[3]})-[:DERIVED_FROM]->(c)
            CREATE (r)-[:HAS_SUBJECT]->(s), (r)-[:APPLIES_TO]->(o), (r)-[:OF_KIND]->(old)
            """,
            p=p,
            lamps=LAMPS,
        )
        writer = GraphWriter(graph)
        kinds = KindVocabulary(writer, _Embedder(), threshold=2.0, index="none")
        # The labels are mapped by rule: the embedding fallback (no index here) is unused.
        rows = await writer.restrictions_for_consolidation()
        mine = [row for row in rows if row["id"].startswith(p)]
        assert {row["id"]: row["shared"] for row in mine} == {
            p + "-r1": True,
            p + "-r2": True,
            p + "-r3": False,
            p + "-r4": True,
        }

        await consolidate(writer, kinds)

        stored = {
            row["id"]: row
            for row in await graph.run(
                """
                MATCH (r:Restriction) WHERE r.id STARTS WITH $p
                MATCH (r)-[:OF_KIND]->(k:RestrictionKind)
                RETURN r.id AS id, r.kind AS kind, k.name AS edge, r.kind_label AS label,
                       r.duplicate_group AS duplicate_group, r.norm_key AS norm_key
                """,
                p=p,
            )
        }
        assert {row["kind"] for row in stored.values()} == {"требование_к_объекту"}
        assert all(row["edge"] == row["kind"] for row in stored.values())
        assert stored[p + "-r1"]["label"] == "требование_хранения_" + p
        assert stored[p + "-r1"]["duplicate_group"] == p + "-r1"
        assert stored[p + "-r2"]["duplicate_group"] == p + "-r1"
        assert stored[p + "-r3"]["duplicate_group"] is None
        assert stored[p + "-r4"]["duplicate_group"] is None
        # The coined kind is unused now and removed.
        assert not await graph.run(
            "MATCH (k:RestrictionKind {name: $name}) RETURN k",
            name="требование_хранения_" + p,
        )

        # A new extraction finds its duplicates by the stored key, in the shared corpus.
        candidates = await writer.duplicate_candidates(
            stored[p + "-r1"]["norm_key"], "new", doc_id=p + "-b"
        )
        assert [row["id"] for row in candidates] == [p + "-r1", p + "-r2", p + "-r4"]
        assert not await writer.duplicate_candidates(
            stored[p + "-r1"]["norm_key"], "new", doc_id=p + "-u"
        )

        members = await GraphReader(graph).duplicate_members([p + "-r1"])
        assert [(m["id"], m["numbering"]) for m in members] == [
            (p + "-r1", "2.8.10"),
            (p + "-r2", "10"),
        ]

        repo = AdminRepository(graph)
        grouped = await repo.restriction_search(query=p, duplicates="grouped")
        assert sorted(item["id"] for item in grouped["items"]) == [p + "-r1", p + "-r2"]
        assert grouped["items"][0]["duplicates"] == 1
        facets = await repo.restriction_facets()
        assert facets["duplicates"]["groups"] >= 1
        card = await repo.restriction(p + "-r1")
        assert [d["id"] for d in card["duplicates"]] == [p + "-r2"]
        assert card["restriction"]["kind_label"] == "требование_хранения_" + p
    finally:
        await graph.run(
            """
            MATCH (n) WHERE n.doc_id STARTS WITH $p OR n.node_id STARTS WITH $p
                         OR n.id STARTS WITH $p OR n.normalized STARTS WITH $p
                         OR n.name ENDS WITH $p
            DETACH DELETE n
            """,
            p=p,
        )
        await graph.close()

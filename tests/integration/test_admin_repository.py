"""Run against an explicitly supplied disposable Neo4j; never use application data."""

import os
import uuid

import pytest

from src.admin_service.repository import AdminRepository
from src.graph.client import Neo4jClient

pytestmark = pytest.mark.integration


async def test_admin_states_counts_filters_and_pagination():
    uri = os.environ.get("NG_ADMIN_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("Set NG_ADMIN_TEST_NEO4J_URI to a disposable Neo4j")
    graph = Neo4jClient(uri, "neo4j", os.environ["NG_ADMIN_TEST_NEO4J_PASSWORD"])
    prefix = "admin-test-" + uuid.uuid4().hex
    ids = [prefix + suffix for suffix in ("-a", "-b", "-c", "-d")]
    try:
        await graph.run(
            """
            CREATE (a:Document {doc_id: $ids[0], name: $name, extraction_incomplete: false}),
                   (b:Document {doc_id: $ids[1], name: $name, extraction_incomplete: true,
                                extraction_failed_clause_ids: ['failed']}),
                   (c:Document {doc_id: $ids[2], name: $name}),
                   (d:Document {doc_id: $ids[3], name: $name})
            CREATE (:Clause {node_id: $ids[0] + '-1', text: 'first', embedding: [1.0]})-[:IN_DOCUMENT]->(a),
                   (:Clause {node_id: $ids[0] + '-2', text: 'second'})-[:IN_DOCUMENT]->(a),
                   (:Clause {node_id: $ids[1] + '-1'})-[:IN_DOCUMENT]->(b),
                   (:Clause {node_id: $ids[2] + '-1'})-[:IN_DOCUMENT]->(c)
            CREATE (:Restriction {id: $ids[2] + '-r', doc_id: $ids[2], embedding: [1.0]}),
                   (:Restriction {id: $ids[1] + '-r1', doc_id: $ids[1]}),
                   (:Restriction {id: $ids[1] + '-r2', doc_id: $ids[1]})
            """,
            ids=ids,
            name=prefix,
        )
        repo = AdminRepository(graph)
        first = await repo.documents(query=prefix.upper(), limit=2)
        assert [d["state"] for d in first["items"]] == ["complete", "incomplete"]
        assert first["items"][0]["clauses"] == 2
        assert first["items"][0]["restrictions"] == 0
        assert first["items"][1]["restrictions"] == 2
        second = await repo.documents(query=prefix, after=first["next_after"], limit=2)
        assert [d["state"] for d in second["items"]] == ["unknown", "no_clauses"]
        assert second["has_more"] is False
        assert len((await repo.documents(query=prefix, state="complete"))["items"]) == 1
        assert (await repo.document(ids[1]))["extraction_failed_clause_ids"] == [
            "failed"
        ]
        assert await repo.document(prefix + "-missing") is None
        clauses = await repo.clauses(ids[0], limit=1)
        assert clauses["items"][0]["text"] == "first"
        assert "embedding" not in clauses["items"][0]
        assert (await repo.clauses(ids[0], after=clauses["next_after"]))["items"][0][
            "text"
        ] == "second"
        restrictions = await repo.restrictions(ids[1], limit=1)
        assert restrictions["has_more"]
        assert (
            len(
                (await repo.restrictions(ids[1], after=restrictions["next_after"]))[
                    "items"
                ]
            )
            == 1
        )
        assert "embedding" not in (await repo.restrictions(ids[2]))["items"][0]
    finally:
        await graph.run(
            "MATCH (n) WHERE n.doc_id IN $ids OR n.node_id STARTS WITH $prefix DETACH DELETE n",
            ids=ids,
            prefix=prefix,
        )
        await graph.close()

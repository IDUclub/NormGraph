"""Integration: delete / prune graph lifecycle against a live Neo4j — self-skips when down."""

from __future__ import annotations

import uuid

import pytest

from src.common.config import settings
from src.graph import Neo4jClient
from src.graph.reader import GraphReader
from src.graph.schema import ensure_schema
from src.graph.writer import GraphWriter


async def _seed(w: GraphWriter, tag: str) -> str:
    """A document with two clauses; one carries a restriction. Returns the doc_id."""
    doc = f"doc-{tag}"
    await w.upsert_document({"doc_id": doc, "name": f"СП {tag}", "content_hash": "h1"})
    await w.upsert_clause(
        {"node_id": f"keep-{tag}", "doc_id": doc, "order": 0, "text": "keep"}
    )
    await w.upsert_clause(
        {"node_id": f"stale-{tag}", "doc_id": doc, "order": 1, "text": "stale"}
    )
    await w.ensure_kind(f"kind_{tag}")
    await w.upsert_entity(f"subj-{tag}", name="s")
    await w.upsert_entity(f"obj-{tag}", name="o")
    await w.upsert_restriction(
        {"id": f"r-{tag}", "subject": "S", "object": "O", "doc_id": doc},
        clause_node_id=f"stale-{tag}",
        subject_normalized=f"subj-{tag}",
        object_normalized=f"obj-{tag}",
        kind_name=f"kind_{tag}",
    )
    return doc


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_and_prune_lifecycle():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    tag = uuid.uuid4().hex[:8]
    w = GraphWriter(client)
    try:
        await ensure_schema(client, settings)
        doc = await _seed(w, tag)

        # Pruning to keep only "keep-*" drops the stale clause and its restriction.
        pruned = await w.prune_clauses(doc, [f"keep-{tag}"])
        assert pruned == 1
        rows = await client.run(
            "MATCH (c:Clause)-[:IN_DOCUMENT]->(:Document {doc_id:$d}) "
            "RETURN collect(c.node_id) AS ids",
            d=doc,
        )
        assert rows[0]["ids"] == [f"keep-{tag}"]
        assert (
            await client.run("MATCH (r:Restriction {id:$id}) RETURN r", id=f"r-{tag}")
            == []
        )

        # documents_by_name / stored_documents surface the document for reconcile.
        by_name = await w.documents_by_name(f"СП {tag}")
        assert any(d["doc_id"] == doc for d in by_name)
        stored = {r["doc_id"]: r for r in await w.stored_documents()}
        assert stored[doc]["content_hash"] == "h1"

        # A fresh restriction, then delete_restrictions_of_doc wipes it.
        await w.upsert_restriction(
            {"id": f"r2-{tag}", "subject": "S", "object": "O", "doc_id": doc},
            clause_node_id=f"keep-{tag}",
            subject_normalized=f"subj-{tag}",
            object_normalized=f"obj-{tag}",
            kind_name=f"kind_{tag}",
        )
        assert await w.delete_restrictions_of_doc(doc) == 1
        assert (
            await client.run("MATCH (r:Restriction {doc_id:$d}) RETURN r", d=doc) == []
        )

        # delete_document removes the document and its remaining clause.
        counts = await w.delete_document(doc)
        assert counts["clauses"] == 1
        assert await client.run("MATCH (d:Document {doc_id:$d}) RETURN d", d=doc) == []
    finally:
        await client.run(
            "MATCH (n) WHERE n.doc_id = $doc OR n.node_id ENDS WITH $tag "
            "OR n.normalized ENDS WITH $tag OR n.name = $kind OR n.id ENDS WITH $tag "
            "DETACH DELETE n",
            doc=f"doc-{tag}",
            tag=tag,
            kind=f"kind_{tag}",
        )
        await client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reextraction_preserves_check_plan_revision_history():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    tag = uuid.uuid4().hex[:8]
    restriction_id = f"r-{tag}"
    writer = GraphWriter(client)
    reader = GraphReader(client)
    plan = {
        "schema_version": "1.0",
        "template": "distance_from_source",
        "template_version": 1,
        "params": {"distance_m": 50},
        "declared_requirements": None,
        "source": {"restriction_id": restriction_id},
        "planner_status": "auto",
    }
    try:
        await ensure_schema(client, settings)
        doc = await _seed(writer, tag)
        await writer.append_check_plan_revision(
            restriction_id, plan, review_status="pending"
        )
        plan["planner_status"] = "reviewed"
        await writer.append_check_plan_revision(
            restriction_id, plan, review_status="approved"
        )

        assert await writer.delete_restrictions_of_doc(doc) == 1
        await writer.upsert_restriction(
            {
                "id": restriction_id,
                "subject": "S",
                "object": "O",
                "doc_id": doc,
            },
            clause_node_id=f"stale-{tag}",
            subject_normalized=f"subj-{tag}",
            object_normalized=f"obj-{tag}",
            kind_name=f"kind_{tag}",
        )

        revisions = await reader.check_plan_revisions(restriction_id)
        assert [item["revision"] for item in revisions] == [2, 1]
        linked = await client.run(
            "MATCH (:Restriction {id:$id})-[:HAS_CHECK_PLAN]->(cp:CheckPlan) "
            "RETURN count(cp) AS count",
            id=restriction_id,
        )
        assert linked[0]["count"] == 2
    finally:
        await client.run(
            "MATCH (n) WHERE n.doc_id = $doc OR n.node_id ENDS WITH $tag "
            "OR n.normalized ENDS WITH $tag OR n.name = $kind OR n.id ENDS WITH $tag "
            "OR n.restriction_id ENDS WITH $tag DETACH DELETE n",
            doc=f"doc-{tag}",
            tag=tag,
            kind=f"kind_{tag}",
        )
        await client.close()

"""Integration: the restriction write layer against a live Neo4j — self-skips when down."""

from __future__ import annotations

import uuid

import pytest

from src.common.config import settings
from src.graph import Neo4jClient
from src.graph.schema import ensure_schema
from src.graph.writer import GraphWriter


def _vec(seed: int) -> list[float]:
    # A distinct unit-ish vector per seed; seeds are made unique per test run (from the
    # tag) so the shared DB's other entities never collide with this run's nearest query.
    v = [0.0] * settings.vector_size
    for i in range(16):
        v[(seed * 131 + i * 7) % settings.vector_size] = 1.0 / (i + 1)
    return v


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restriction_layer_roundtrip():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    tag = uuid.uuid4().hex[:8]
    base = int(tag, 16) % 1_000_000  # unique vector seeds per run
    w = GraphWriter(client)
    try:
        await ensure_schema(client, settings)
        doc = f"doc-{tag}"
        await w.upsert_document({"doc_id": doc, "name": "СП test"})
        await w.upsert_clause({"node_id": f"c-{tag}", "doc_id": doc, "text": "t"})
        await w.ensure_kind(f"kind_{tag}", embedding=_vec(base + 1))
        await w.upsert_entity(f"subj-{tag}", name="subj", embedding=_vec(base + 2))
        await w.upsert_entity(f"obj1-{tag}", name="o1", embedding=_vec(base + 3))
        await w.upsert_entity(f"obj2-{tag}", name="o2", embedding=_vec(base + 4))

        for rid, obj in ((f"r1-{tag}", f"obj1-{tag}"), (f"r2-{tag}", f"obj2-{tag}")):
            await w.upsert_restriction(
                {"id": rid, "subject": "S", "object": "O", "kind": f"kind_{tag}"},
                clause_node_id=f"c-{tag}",
                subject_normalized=f"subj-{tag}",
                object_normalized=obj,
                kind_name=f"kind_{tag}",
                embedding=_vec(base + 10),
            )
        linked = await w.link_shares_entity(f"r1-{tag}")
        assert linked == 1  # shares the subject entity with r2

        near = await w.nearest(settings.entity_vector_index, _vec(base + 2), k=1)
        assert near and near[0]["normalized"] == f"subj-{tag}"

        edges = await client.run(
            "MATCH (r:Restriction {id:$id})-[e]->() RETURN collect(DISTINCT type(e)) AS t",
            id=f"r1-{tag}",
        )
        assert set(edges[0]["t"]) >= {
            "DERIVED_FROM",
            "HAS_SUBJECT",
            "APPLIES_TO",
            "OF_KIND",
        }
    finally:
        await client.run(
            "MATCH (n) WHERE n.id ENDS WITH $tag OR n.doc_id = $doc "
            "OR n.node_id ENDS WITH $tag OR n.normalized ENDS WITH $tag "
            "OR n.name = $kind DETACH DELETE n",
            tag=tag,
            doc=f"doc-{tag}",
            kind=f"kind_{tag}",
        )
        await client.close()

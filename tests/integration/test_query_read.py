"""Integration: restriction query read layer against a live Neo4j — self-skips when down."""

from __future__ import annotations

import uuid

import pytest

from src.common.config import settings
from src.dvd_client.models import DocumentRef
from src.graph import Neo4jClient
from src.graph.reader import GraphReader
from src.graph.schema import ensure_schema
from src.graph.writer import GraphWriter
from src.query.service import QueryService


def _vec(seed: int) -> list[float]:
    v = [0.0] * settings.vector_size
    for i in range(16):
        v[(seed * 131 + i * 7) % settings.vector_size] = 1.0 / (i + 1)
    return v


class _Embedder:
    model = "tiny"
    dim = settings.vector_size

    async def embed_documents(self, texts):
        return [_vec(len(t)) for t in texts]

    async def embed_query(self, text):
        return _vec(len(text))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_read_layer():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    t = uuid.uuid4().hex[:8]
    base = int(t, 16) % 1_000_000
    kind = f"kind_{t}"
    tag = f"tag_{t}"
    w = GraphWriter(client)
    reader = GraphReader(client)
    try:
        await ensure_schema(client, settings)
        await w.upsert_document({"doc_id": f"d1-{t}", "name": f"СП {t}"})
        await w.upsert_document({"doc_id": f"d2-{t}", "name": f"СП2 {t}"})
        await w.upsert_clause(
            {"node_id": f"c1-{t}", "doc_id": f"d1-{t}", "text": "x", "tags": [tag]}
        )
        await w.upsert_clause({"node_id": f"c2-{t}", "doc_id": f"d2-{t}", "text": "y"})
        await w.link_reference(
            f"c1-{t}",
            DocumentRef(target_node_id=f"c2-{t}", scope="external", resolved=True),
        )
        await w.ensure_kind(kind, embedding=_vec(base + 1))
        await w.upsert_entity(f"subj-{t}", name="s", embedding=_vec(base + 2))
        await w.upsert_entity(f"obj-{t}", name="o", embedding=_vec(base + 3))

        async def restr(rid, subj, obj, clause, e):
            await w.upsert_restriction(
                {"id": rid, "subject": subj, "object": obj, "kind": kind},
                clause_node_id=clause,
                subject_normalized=subj,
                object_normalized=obj,
                kind_name=kind,
                embedding=_vec(e),
            )
            await w.link_shares_entity(rid)

        await restr(f"r1-{t}", f"subj-{t}", f"obj-{t}", f"c1-{t}", base + 10)
        await restr(f"r2-{t}", f"subj-{t}", f"obj-{t}", f"c2-{t}", base + 11)

        # filter by tag finds only the clause carrying it
        by_tag = await reader.search_filter({"tags": [tag]}, limit=10)
        assert {r["id"] for r in by_tag} == {f"r1-{t}"}

        # neighbours: r1 and r2 share both entities, and are reference-linked via c1→c2
        nb = await reader.neighbors([f"r1-{t}"])
        assert {row["neighbor_id"] for row in nb} == {f"r2-{t}"}
        assert {row["relation"] for row in nb} == {"shares_entity", "reference"}

        # applicable to the object entity returns both
        appl = await reader.applicable([f"obj-{t}"], {}, limit=10)
        assert {r["id"] for r in appl} == {f"r1-{t}", f"r2-{t}"}

        svc = QueryService(reader, _Embedder(), None, settings)
        graph = await svc.graph(f"r1-{t}", depth=2)
        assert {n.id for n in graph.nodes} == {f"r1-{t}", f"r2-{t}"}
    finally:
        await client.run(
            "MATCH (n) WHERE n.id ENDS WITH $t OR n.doc_id ENDS WITH $t "
            "OR n.node_id ENDS WITH $t OR n.normalized ENDS WITH $t OR n.name = $k "
            "DETACH DELETE n",
            t=t,
            k=kind,
        )
        await client.close()

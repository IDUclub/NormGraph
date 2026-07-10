"""Integration: the idempotency-guard read (document_sync_state) against a live Neo4j."""

from __future__ import annotations

import uuid

import pytest

from src.common.config import settings
from src.graph import Neo4jClient
from src.graph.writer import GraphWriter


@pytest.mark.integration
@pytest.mark.asyncio
async def test_document_sync_state():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    t = uuid.uuid4().hex[:8]
    w = GraphWriter(client)
    try:
        # absent document → None
        assert await w.document_sync_state(f"missing-{t}") is None

        await w.upsert_document(
            {"doc_id": f"d-{t}", "name": "СП", "content_hash": "h1"}
        )
        await w.upsert_clause({"node_id": f"c-{t}", "doc_id": f"d-{t}", "text": "x"})
        # ingested but not extracted yet → hash present, 0 restrictions
        state = await w.document_sync_state(f"d-{t}")
        assert state["content_hash"] == "h1" and state["restrictions"] == 0

        await w.ensure_kind(f"k-{t}")
        await w.upsert_entity(f"s-{t}", name="s")
        await w.upsert_entity(f"o-{t}", name="o")
        await w.upsert_restriction(
            {
                "id": f"r-{t}",
                "subject": "S",
                "object": "O",
                "kind": f"k-{t}",
                "doc_id": f"d-{t}",
            },
            clause_node_id=f"c-{t}",
            subject_normalized=f"s-{t}",
            object_normalized=f"o-{t}",
            kind_name=f"k-{t}",
        )
        # now the guard sees a synced doc: same hash + a restriction
        state = await w.document_sync_state(f"d-{t}")
        assert state["content_hash"] == "h1" and state["restrictions"] == 1
    finally:
        await client.run(
            "MATCH (n) WHERE n.id ENDS WITH $t OR n.doc_id ENDS WITH $t "
            "OR n.node_id ENDS WITH $t OR n.normalized ENDS WITH $t OR n.name = $k "
            "DETACH DELETE n",
            t=t,
            k=f"k-{t}",
        )
        await client.close()

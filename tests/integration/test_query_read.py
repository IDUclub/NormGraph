"""Integration: restriction query read layer against a live Neo4j — self-skips when down."""

from __future__ import annotations

import uuid

import pytest

from src.common.config import settings
from src.dto.query import (
    DocumentListRequest,
    EntityResolveRequest,
    RestrictionListRequest,
)
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

        # keyset pages by id cover the corpus once; executable_only needs a usable plan
        first = await reader.list_page({"kind": kind}, after_id=None, limit=1)
        rest = await reader.list_page({"kind": kind}, after_id=first[0]["id"], limit=10)
        assert [r["id"] for r in first + rest] == [f"r1-{t}", f"r2-{t}"]
        for rid, status in ((f"r1-{t}", "unsupported"), (f"r2-{t}", "auto")):
            await w.append_check_plan_revision(
                rid,
                {
                    "schema_version": "1.0",
                    "template": "unsupported",
                    "template_version": 1,
                    "params": {},
                    "source": {"restriction_id": rid},
                    "planner_status": status,
                },
                review_status="pending",
            )
        executable = await reader.list_page(
            {"kind": kind}, after_id=None, limit=10, executable_only=True
        )
        assert [r["id"] for r in executable] == [f"r2-{t}"]
        assert executable[0]["check_planner_status"] == "auto"
        by_kinds = await reader.list_page(
            {"kinds": [kind, "other_kind"]}, after_id=None, limit=10
        )
        assert {r["id"] for r in by_kinds} >= {f"r1-{t}", f"r2-{t}"}
        assert not await reader.list_page(
            {"kinds": ["other_kind"], "doc_id": f"d1-{t}"}, after_id=None, limit=10
        )

        # vectors written before the sentence joined the embedding text are refreshed once
        stale = await reader.restrictions_with_stale_embedding(
            version=2, after_id=f"r0-{t}", limit=10
        )
        assert [r["id"] for r in stale][:2] == [f"r1-{t}", f"r2-{t}"]
        await w.set_restriction_embeddings(
            [{"id": f"r1-{t}", "embedding": _vec(base + 20)}], version=2
        )
        stale = await reader.restrictions_with_stale_embedding(
            version=2, after_id=f"r0-{t}", limit=10
        )
        assert f"r1-{t}" not in {r["id"] for r in stale}

        svc = QueryService(reader, _Embedder(), None, settings)
        graph = await svc.graph(f"r1-{t}", depth=2)
        assert {n.id for n in graph.nodes} == {f"r1-{t}", f"r2-{t}"}
    finally:
        await client.run(
            "MATCH (n) WHERE n.id ENDS WITH $t OR n.doc_id ENDS WITH $t "
            "OR n.node_id ENDS WITH $t OR n.normalized ENDS WITH $t OR n.name = $k "
            "OR n.restriction_id ENDS WITH $t "
            "DETACH DELETE n",
            t=t,
            k=kind,
        )
        await client.close()


def _plan(rid: str, status: str, entities: list[str]) -> dict:
    return {
        "schema_version": "1.0",
        "template": "distance_from_source",
        "template_version": 1,
        "params": {},
        "declared_requirements": {
            "layers": [
                {"role": f"layer_{i}", "entity": entity, "entity_type": "service"}
                for i, entity in enumerate(entities)
            ]
        },
        "source": {"restriction_id": rid},
        "planner_status": status,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topic_filter_documents_and_entity_candidates():
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
    school, home, zone = f"школа-{t}", f"дом-{t}", f"зона-{t}"
    w = GraphWriter(client)
    try:
        await ensure_schema(client, settings)
        await w.upsert_document({"doc_id": f"d1-{t}", "name": f"СП {t}"})
        await w.upsert_document({"doc_id": f"d2-{t}", "name": f"СанПиН {t}"})
        await w.upsert_document(
            {
                "doc_id": f"du-{t}",
                "name": f"Мой документ {t}",
                "user_id": f"u-{t}",
                "scenario_id": "1",
            }
        )
        for clause, doc in (("c1", "d1"), ("c2", "d2"), ("cu", "du")):
            await w.upsert_clause(
                {"node_id": f"{clause}-{t}", "doc_id": f"{doc}-{t}", "text": "x"}
            )
        await w.ensure_kind(kind, embedding=_vec(base + 1))
        await w.upsert_entity(
            school, name="Школа", aliases=[f"школы-{t}"], embedding=_vec(base + 2)
        )
        await w.upsert_entity(home, name="Дом", embedding=_vec(base + 3))
        await w.upsert_entity(zone, name="Зона", embedding=_vec(base + 4))

        async def restr(rid, subj, obj, clause):
            await w.upsert_restriction(
                {"id": rid, "subject": subj, "object": obj, "kind": kind},
                clause_node_id=clause,
                subject_normalized=subj,
                object_normalized=obj,
                kind_name=kind,
                embedding=_vec(base + 10),
            )

        # r1 names the school as its subject; r2 only through its plan layer
        # (label as written in the clause); r3 is unrelated; ru sits in a user index.
        await restr(f"r1-{t}", school, home, f"c1-{t}")
        await restr(f"r2-{t}", zone, home, f"c2-{t}")
        await restr(f"r3-{t}", zone, home, f"c2-{t}")
        await restr(f"ru-{t}", school, home, f"cu-{t}")
        await w.append_check_plan_revision(
            f"r1-{t}", _plan(f"r1-{t}", "auto", ["Школа-" + t]), review_status="pending"
        )
        await w.append_check_plan_revision(
            f"r2-{t}",
            _plan(f"r2-{t}", "reviewed", [f"Школы-{t}", "Дом-" + t]),
            review_status="approved",
        )
        await w.append_check_plan_revision(
            f"r3-{t}", _plan(f"r3-{t}", "auto", ["Дом-" + t]), review_status="pending"
        )

        svc = QueryService(GraphReader(client), _Embedder(), None, settings)
        page = await svc.list_page(
            RestrictionListRequest(kind=kind, entities=[f"Школы-{t}"])
        )
        assert [hit.id for hit in page.hits] == [f"r1-{t}", f"r2-{t}", f"ru-{t}"]

        documents = await svc.list_documents(
            DocumentListRequest(kind=kind, entities=[school], executable_only=True)
        )
        assert [
            (d.doc_id, d.restriction_count, d.executable_count)
            for d in documents.documents
        ] == [(f"d1-{t}", 1, 1), (f"d2-{t}", 1, 1)]
        everything = await svc.list_documents(DocumentListRequest(kind=kind))
        assert {d.doc_id: d.executable_count for d in everything.documents} == {
            f"d2-{t}": 2,
            f"d1-{t}": 1,
        }

        [resolution] = await svc.resolve_entities(
            EntityResolveRequest(terms=[f"Школы-{t}"])
        )
        by_name = {c.normalized: c for c in resolution.candidates}
        assert by_name[school].match == "alias"
        assert (
            by_name[school].restriction_count,
            by_name[school].executable_count,
        ) == (
            2,
            1,
        )

        # Plans stored before layer_entities existed are keyed once, then skipped.
        await client.run(
            "MATCH (cp:CheckPlan {restriction_id: $rid}) REMOVE cp.layer_entities",
            rid=f"r2-{t}",
        )
        assert await w.backfill_check_plan_layer_entities() >= 1
        assert await w.backfill_check_plan_layer_entities() == 0
        page = await svc.list_page(RestrictionListRequest(kind=kind, entities=[school]))
        assert f"r2-{t}" in {hit.id for hit in page.hits}
    finally:
        await client.run(
            "MATCH (n) WHERE n.id ENDS WITH $t OR n.doc_id ENDS WITH $t "
            "OR n.node_id ENDS WITH $t OR n.normalized ENDS WITH $t OR n.name = $k "
            "OR n.restriction_id ENDS WITH $t "
            "DETACH DELETE n",
            t=t,
            k=kind,
        )
        await client.close()

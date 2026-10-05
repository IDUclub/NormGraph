"""Integration: explanation links against a live Neo4j — self-skips when it is down."""

from __future__ import annotations

import uuid

import pytest

from src.common.config import settings
from src.dvd_client.models import SearchHit, SearchResponse
from src.graph import Neo4jClient
from src.graph.context import load_clause_contexts
from src.graph.schema import ensure_schema
from src.graph.writer import GraphWriter
from src.ingestion.explanations import ExplanationLinker
from src.pipeline.reuse import extraction_hash

SIMILAR = (
    "Минимальный отступ от границы земельного участка определяется от наружной стены "
    "здания без учёта крыльца и козырька."
)


class FakeDVD:
    """IDU_DVD search: the letter's long clause is nearest to the setback clause."""

    def __init__(self, hits: list[tuple[str, float]], doc_id: str) -> None:
        self.hits, self.doc_id = hits, doc_id
        self.queries: list[str] = []

    async def search(self, query, *, doc_id=None, limit=10, related=True, **_):
        assert doc_id == self.doc_id and related is False
        self.queries.append(query)
        return SearchResponse(
            hits=[
                SearchHit(id=node, score=score, doc_id=doc_id, name="ПЗЗ")
                for node, score in self.hits[:limit]
            ]
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explanations_are_linked_shown_carried_and_dropped_live():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    tag = uuid.uuid4().hex[:8]
    w = GraphWriter(client)
    rules, letter = f"rules-{tag}", f"letter-{tag}"
    height, setback = f"r5-{tag}", f"r6-{tag}"
    cites, explains = f"l1-{tag}", f"l2-{tag}"
    try:
        await ensure_schema(client, settings)
        await w.upsert_document({"doc_id": rules, "name": f"ПЗЗ {tag}"})
        await w.upsert_document(
            {"doc_id": letter, "name": f"Письмо {tag}", "explains": f"ПЗЗ {tag}"}
        )
        for node, doc, numbering, text in [
            (height, rules, "5", "Предельная высота зданий — 15 м."),
            (setback, rules, "6", "Минимальный отступ от границ участка — 3 м."),
            (cites, letter, "1", "По пункту 5: высота считается до конька кровли."),
            (explains, letter, "2", SIMILAR),
        ]:
            await w.upsert_clause(
                {"node_id": node, "doc_id": doc, "name": f"{doc}", "numbering": numbering,
                 "text": text}
            )
        await client.run(
            "MATCH (a:Clause {node_id: $a}), (b:Clause {node_id: $b}) "
            "MERGE (a)-[:REFERENCES {raw: 'пункту 5'}]->(b)",
            a=cites,
            b=height,
        )
        dvd = FakeDVD([(setback, 0.82), (height, 0.41)], rules)
        linker = ExplanationLinker(dvd, w, min_score=0.6, per_clause=2)

        # The letter cites clause 5 and is nearest to clause 6 by meaning.
        assert await linker.link(letter) == {rules: sorted([height, setback])}
        assert dvd.queries == [SIMILAR]  # a citing clause is not searched for
        edges = await client.run(
            "MATCH (e:Clause)-[x:EXPLAINS]->(t:Clause) WHERE e.doc_id = $d "
            "RETURN e.node_id AS e, t.node_id AS t, x.via AS via ORDER BY e",
            d=letter,
        )
        assert edges == [
            {"e": cites, "t": height, "via": "reference"},
            {"e": explains, "t": setback, "via": "similar"},
        ]
        # Relinking the same text changes nothing; the rules see the same pair.
        assert await linker.link(letter) == {}
        assert await linker.link(rules) == {}

        contexts = await load_clause_contexts(
            client, "MATCH (c:Clause {node_id: $id})\n", id=height
        )
        (item,) = contexts[height].related
        assert (item.relation, item.node_id) == ("explanation", cites)

        # A new edition of the rules: the identical clause keeps its explanation.
        await w.mark_extracted(
            [{"node_id": height, "hash": extraction_hash("Предельная высота зданий — 15 м.")}]
        )
        successor = f"r5b-{tag}"
        await w.upsert_clause(
            {"node_id": successor, "doc_id": rules, "numbering": "5",
             "text": "Предельная высота зданий — 15 м."}
        )
        keep = [successor, setback]
        assert await w.carry_unchanged_clauses(rules, keep, extraction_hash) == [successor]
        await w.prune_clauses(rules, keep)
        assert await linker.link(rules) == {}
        moved = await client.run(
            "MATCH (:Clause {node_id: $e})-[x:EXPLAINS]->(t:Clause) RETURN t.node_id AS t",
            e=cites,
        )
        assert moved == [{"t": successor}]

        # Unlinked in IDU_DVD: the explained clauses lose it and are reported.
        await w.upsert_document({"doc_id": letter, "explains": None})
        assert await linker.link(letter) == {rules: sorted([setback, successor])}
        assert (
            await client.run(
                "MATCH ()-[x:EXPLAINS]->(:Clause {doc_id: $d}) RETURN x", d=rules
            )
            == []
        )
    finally:
        await w.delete_document(letter)
        await w.delete_document(rules)
        await client.close()

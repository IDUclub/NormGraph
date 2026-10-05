"""Integration: zone regulations stored and queried in a live Neo4j — self-skips when down."""

from __future__ import annotations

import json
import uuid

import pytest

from src.common.config import settings
from src.graph import Neo4jClient
from src.graph.schema import ensure_schema
from src.graph.writer import GraphWriter
from src.regulations.parser import PermittedUse, ZoneParameter, ZoneRegulation
from src.regulations.store import ZoneStore


def _zone(code, *uses):
    return ZoneRegulation(
        code=code,
        name=f"ЗОНА {code}",
        uses=[PermittedUse(section=s, name=n, codes=[c]) for s, n, c in uses],
        parameters=[ZoneParameter(name="Максимальная высота", kind="max_height", value=15.0)],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_zones_are_replaced_and_found_by_territory_code_and_use_live():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")

    tag = uuid.uuid4().hex[:8]
    doc, territory = f"pzz-{tag}", 900000 + int(tag[:4], 16)
    w, store = GraphWriter(client), ZoneStore(client)
    try:
        await ensure_schema(client, settings)
        await w.upsert_document(
            {"doc_id": doc, "name": f"ПЗЗ {tag}", "territory_id": territory}
        )
        zones = [
            _zone("Ж-1", ("main", "ИЖС", "2.1"), ("conditional", "Магазины", "4.4")),
            _zone("О-1", ("main", "Магазины", "4.4")),
        ]
        assert await store.replace(doc, zones) == 2

        found = await store.zones(territory_ids=[territory])
        assert [json.loads(r["regulation"])["code"] for r in found] == ["Ж-1", "О-1"]
        assert found[0]["doc_id"] == doc and found[0]["territory_id"] == territory
        by_use = await store.zones(territory_ids=[territory], vri_code="2.1")
        assert [json.loads(r["regulation"])["code"] for r in by_use] == ["Ж-1"]
        by_code = await store.zones(doc_id=doc, codes=["О-1"])
        assert len(by_code) == 1
        (listed,) = [d for d in await store.documents([territory])]
        assert listed["zones"] == 2

        # A new edition replaces the zones; a document that is no ПЗЗ any more has none.
        assert await store.replace(doc, zones[:1]) == 1
        assert len(await store.zones(doc_id=doc)) == 1
        assert await store.replace(doc, []) == 0
        assert await store.zones(doc_id=doc) == []

        # Deleting the document takes its zones with it.
        await store.replace(doc, zones)
        await w.delete_document(doc)
        assert await client.run("MATCH (z:Zone {doc_id: $d}) RETURN z", d=doc) == []
    finally:
        await w.delete_document(doc)
        await client.run("MATCH (z:Zone {doc_id: $d}) DETACH DELETE z", d=doc)
        await client.close()

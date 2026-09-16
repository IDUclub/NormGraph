"""Revision compare-and-set against Neo4j; only creates uniquely named test nodes."""

import asyncio
import uuid

import pytest

from src.common.config import settings
from src.graph import Neo4jClient
from src.graph.writer import GraphWriter


@pytest.mark.integration
async def test_regeneration_serializes_writers_and_preserves_expert_rejection():
    client = Neo4jClient(
        settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:
        await client.close()
        pytest.skip(f"Neo4j unavailable: {exc}")
    restriction_id = f"test-revision-{uuid.uuid4().hex}"
    writer = GraphWriter(client)
    plan = {
        "schema_version": "1.0",
        "template": "unsupported",
        "template_version": 1,
        "params": {},
        "source": {"restriction_id": restriction_id},
        "planner_status": "unsupported",
    }
    try:
        await client.run("CREATE (:Restriction {id: $id})", id=restriction_id)

        async def regenerate(expected):
            return await writer.append_check_plan_revision(
                restriction_id,
                plan,
                review_status="rejected",
                protect_reviewed=True,
                expected_revision=expected,
            )

        assert await regenerate(0) == 1
        results = await asyncio.gather(regenerate(1), regenerate(1))
        assert sorted(results, key=lambda value: value or 0) == [None, 2]
        assert await regenerate(1) is None
        # A rejection carries an author even when planner_status is not "reviewed".
        assert (
            await writer.append_check_plan_revision(
                restriction_id,
                plan,
                review_status="rejected",
                author="test-expert",
            )
            == 3
        )
        assert await regenerate(3) is None
        rows = await client.run(
            "MATCH (cp:CheckPlan {restriction_id: $id}) "
            "RETURN cp.revision AS revision, cp.current AS current, cp.author AS author "
            "ORDER BY revision",
            id=restriction_id,
        )
        assert rows == [
            {"revision": 1, "current": False, "author": None},
            {"revision": 2, "current": False, "author": None},
            {"revision": 3, "current": True, "author": "test-expert"},
        ]
    finally:
        await client.run(
            "MATCH (n) WHERE (n:Restriction AND n.id = $id) "
            "OR (n:CheckPlan AND n.restriction_id = $id) DETACH DELETE n",
            id=restriction_id,
        )
        await client.close()

"""Integration smoke test for Neo4j connectivity — self-skips when the DB is down."""

from __future__ import annotations

import pytest

from src.common.config import settings
from src.graph import Neo4jClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_neo4j_reachable():
    client = Neo4jClient(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        database=settings.neo4j_database,
    )
    try:
        await client.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {exc}")
    finally:
        rows = None
        try:
            rows = await client.run("RETURN 1 AS ok")
        except Exception:
            pass
        await client.close()
    assert rows == [{"ok": 1}]

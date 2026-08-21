from __future__ import annotations

import pytest

from src.graph.reader import GraphReader


class CapturingClient:
    def __init__(self) -> None:
        self.query = ""

    async def run(self, query: str, **params):
        self.query = query
        return []


@pytest.mark.asyncio
async def test_search_filter_applies_filters_before_optional_check_plan_join():
    client = CapturingClient()

    await GraphReader(client).search_filter({"tags": ["schools"]}, limit=10)

    where_position = client.query.index("WHERE ($kind IS NULL")
    check_plan_position = client.query.index("OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]")
    return_position = client.query.index("RETURN r.id AS id")
    assert where_position < check_plan_position < return_position

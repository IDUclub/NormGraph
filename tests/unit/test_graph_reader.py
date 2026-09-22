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


@pytest.mark.asyncio
async def test_check_plan_history_is_read_by_stable_restriction_id():
    client = CapturingClient()

    await GraphReader(client).check_plan_revisions("r1")

    assert "MATCH (cp:CheckPlan {restriction_id: $restriction_id})" in client.query
    assert "HAS_CHECK_PLAN" not in client.query


@pytest.mark.asyncio
async def test_missing_check_plan_page_uses_keyset_and_current_plan_filter():
    client = CapturingClient()

    await GraphReader(client).restrictions_without_current_check_plan(
        after_id="r100", limit=51
    )

    assert "r.id > $after_id" in client.query
    assert "NOT EXISTS" in client.query
    assert "CheckPlan {current: true}" in client.query
    assert "ORDER BY r.id" in client.query

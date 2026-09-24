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


@pytest.mark.asyncio
async def test_list_page_filters_before_optional_check_plan_join():
    client = CapturingClient()

    await GraphReader(client).list_page(
        {}, after_id="r100", limit=201, executable_only=True
    )

    keyset_position = client.query.index("r.id > $after_id")
    executable_position = client.query.index("plan.planner_status IN")
    check_plan_position = client.query.index("OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]")
    assert keyset_position < executable_position < check_plan_position
    assert client.query.rstrip().endswith("ORDER BY r.id\nLIMIT $limit")


@pytest.mark.asyncio
async def test_topic_filter_matches_entities_and_current_plan_layers():
    client = CapturingClient()

    await GraphReader(client).list_page(
        {"entities": ["школа"]}, after_id=None, limit=10
    )

    assert "subj.normalized IN $entities OR obj.normalized IN $entities" in client.query
    assert "topic_plan.layer_entities" in client.query
    topic_position = client.query.index("$entities IS NULL")
    check_plan_position = client.query.index("OPTIONAL MATCH (r)-[:HAS_CHECK_PLAN]")
    assert topic_position < check_plan_position


@pytest.mark.asyncio
async def test_document_listing_hides_user_documents_and_counts_executable():
    client = CapturingClient()

    await GraphReader(client).list_documents(
        {"entities": ["школа"]}, executable_only=True, limit=20
    )

    filters_position = client.query.index("$entities IS NULL")
    user_scope_position = client.query.index("d.user_id IS NULL")
    counts_position = client.query.index("executable_count")
    assert filters_position < user_scope_position < counts_position
    assert "WHERE NOT $executable_only OR executable_count > 0" in client.query


@pytest.mark.asyncio
async def test_entity_keys_keep_the_requested_names_and_add_aliases():
    class AliasClient(CapturingClient):
        async def run(self, query: str, **params):
            self.query = query
            return [{"normalized": "школа", "aliases": ["школы", "школа"]}]

    keys = await GraphReader(AliasClient()).entity_keys(["школы"])

    assert keys == ["школа", "школы"]

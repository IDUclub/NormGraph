"""Graph schema DDL is well-formed and carries the configured vector dimension."""

from __future__ import annotations

import pytest

from src.common.config import Settings
from src.graph.schema import (
    VectorIndexDimensionMismatch,
    ensure_schema,
    schema_statements,
)


class _SchemaClient:
    def __init__(self, dimensions: dict[str, int]) -> None:
        self.dimensions = dimensions
        self.queries: list[str] = []

    async def run(self, query: str, **params) -> list[dict]:
        self.queries.append(query)
        if query.startswith("SHOW VECTOR INDEXES"):
            return [
                {
                    "name": name,
                    "options": {"indexConfig": {"vector.dimensions": dimension}},
                }
                for name, dimension in self.dimensions.items()
            ]
        return []


def test_schema_statements_include_constraints_and_vector_dim():
    stmts = schema_statements(Settings(vector_size=2048))
    joined = "\n".join(stmts)
    assert "REQUIRE d.doc_id IS UNIQUE" in joined
    assert "REQUIRE c.node_id IS UNIQUE" in joined
    assert "REQUIRE r.id IS UNIQUE" in joined
    # All four vector indexes carry the configured dimension.
    assert joined.count("`vector.dimensions`: 2048") == 4
    assert "FOR (r:Restriction) ON (r.embedding)" in joined
    assert "FOR (c:Clause) ON (c.embedding)" in joined
    assert "FOR (e:Entity) ON (e.embedding)" in joined
    assert "FOR (k:RestrictionKind) ON (k.embedding)" in joined


def test_vector_dim_follows_setting():
    stmts = schema_statements(Settings(vector_size=1024))
    assert any("`vector.dimensions`: 1024" in s for s in stmts)


@pytest.mark.asyncio
async def test_ensure_schema_accepts_matching_vector_indexes():
    settings = Settings(vector_size=2048)
    client = _SchemaClient(
        {
            settings.restriction_vector_index: 2048,
            settings.clause_vector_index: 2048,
            settings.entity_vector_index: 2048,
            settings.kind_vector_index: 2048,
        }
    )

    await ensure_schema(client, settings)

    assert client.queries[-1].startswith("SHOW VECTOR INDEXES")


@pytest.mark.asyncio
async def test_ensure_schema_rejects_incompatible_existing_vector_indexes():
    settings = Settings(vector_size=2048)
    client = _SchemaClient(
        {
            settings.restriction_vector_index: 1024,
            settings.clause_vector_index: 1024,
            settings.entity_vector_index: 1024,
            settings.kind_vector_index: 1024,
        }
    )

    with pytest.raises(VectorIndexDimensionMismatch, match="expected 2048"):
        await ensure_schema(client, settings)

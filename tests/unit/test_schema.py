"""Graph schema DDL is well-formed and carries the configured vector dimension."""

from __future__ import annotations

from src.common.config import Settings
from src.graph.schema import schema_statements


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

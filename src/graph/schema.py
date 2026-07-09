"""Graph schema: uniqueness constraints and native vector indexes.

Provisioned once at startup (idempotent — every statement is ``IF NOT EXISTS``). The label model:

* ``:Document {doc_id}``        — a logical normative document (stable across versions);
* ``:Clause {node_id}``         — one document fragment/clause (Qdrant point id = ``node_id``);
* ``:Restriction {id}``         — one extracted restriction triple, vector-indexed;
* ``:Entity {normalized}``      — a canonical subject/object entity (deduped across documents);
* ``:RestrictionKind {name}``   — a controlled-vocabulary restriction kind;
* ``:PendingReference {key}``   — a dangling reference target not yet in the store.

Edges (distinct types): ``IN_DOCUMENT``, ``PART_OF``, ``REFERENCES``, ``DERIVED_FROM``,
``HAS_SUBJECT``, ``APPLIES_TO``, ``OF_KIND``, ``SHARES_ENTITY``.
"""

from __future__ import annotations

import structlog

from src.common.config import Settings
from src.graph.client import Neo4jClient

log = structlog.get_logger(__name__)


CONSTRAINTS: list[str] = [
    "CREATE CONSTRAINT document_doc_id IF NOT EXISTS "
    "FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT clause_node_id IF NOT EXISTS "
    "FOR (c:Clause) REQUIRE c.node_id IS UNIQUE",
    "CREATE CONSTRAINT restriction_id IF NOT EXISTS "
    "FOR (r:Restriction) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT entity_normalized IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.normalized IS UNIQUE",
    "CREATE CONSTRAINT kind_name IF NOT EXISTS "
    "FOR (k:RestrictionKind) REQUIRE k.name IS UNIQUE",
    "CREATE CONSTRAINT pending_ref_key IF NOT EXISTS "
    "FOR (p:PendingReference) REQUIRE p.key IS UNIQUE",
]


def vector_index_statements(settings: Settings) -> list[str]:
    """Native vector-index DDL for the restriction and clause embeddings."""
    dim = int(settings.vector_size)
    return [
        f"CREATE VECTOR INDEX {settings.restriction_vector_index} IF NOT EXISTS "
        f"FOR (r:Restriction) ON (r.embedding) "
        f"OPTIONS {{ indexConfig: {{ `vector.dimensions`: {dim}, "
        f"`vector.similarity_function`: 'cosine' }} }}",
        f"CREATE VECTOR INDEX {settings.clause_vector_index} IF NOT EXISTS "
        f"FOR (c:Clause) ON (c.embedding) "
        f"OPTIONS {{ indexConfig: {{ `vector.dimensions`: {dim}, "
        f"`vector.similarity_function`: 'cosine' }} }}",
        f"CREATE VECTOR INDEX {settings.entity_vector_index} IF NOT EXISTS "
        f"FOR (e:Entity) ON (e.embedding) "
        f"OPTIONS {{ indexConfig: {{ `vector.dimensions`: {dim}, "
        f"`vector.similarity_function`: 'cosine' }} }}",
        f"CREATE VECTOR INDEX {settings.kind_vector_index} IF NOT EXISTS "
        f"FOR (k:RestrictionKind) ON (k.embedding) "
        f"OPTIONS {{ indexConfig: {{ `vector.dimensions`: {dim}, "
        f"`vector.similarity_function`: 'cosine' }} }}",
    ]


def schema_statements(settings: Settings) -> list[str]:
    """All DDL statements provisioned at startup, in order."""
    return [*CONSTRAINTS, *vector_index_statements(settings)]


async def ensure_schema(client: Neo4jClient, settings: Settings) -> None:
    """Create constraints + vector indexes if they do not exist (idempotent)."""
    for stmt in schema_statements(settings):
        await client.run(stmt)
    log.info("graph_schema_ensured", vector_dim=settings.vector_size)

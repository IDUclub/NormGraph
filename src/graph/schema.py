"""Graph schema: uniqueness constraints and native vector indexes.

Provisioned once at startup (idempotent — every statement is ``IF NOT EXISTS``). The label model:

* ``:Document {doc_id}``        — a logical normative document (stable across versions);
* ``:Clause {node_id}``         — one document fragment/clause (Qdrant point id = ``node_id``);
* ``:Restriction {id}``         — one extracted restriction triple, vector-indexed;
* ``:Entity {normalized}``      — a canonical subject/object entity (deduped across documents);
* ``:RestrictionKind {name}``   — a controlled-vocabulary restriction kind;
* ``:PendingReference {key}``   — a dangling reference target not yet in the store.

Edges (distinct types): ``IN_DOCUMENT``, ``PART_OF``, ``REFERENCES``, ``DERIVED_FROM``,
``HAS_SUBJECT``, ``APPLIES_TO``, ``OF_KIND``, ``SHARES_ENTITY``, ``CONFLICTS_WITH``.

User-scoped documents (ingested from an IDU_DVD user document index, see ``src/sync``) are
ordinary ``:Document`` nodes carrying three extra optional properties: ``user_id``,
``scenario_id`` (the isolation boundary — matches the Kafka event fields) and ``project_id`` (a
filter tag only). IDU_DVD's ``doc_id`` is a random UUID, unique regardless of scope, so no
constraint changes are needed for user documents to coexist with the shared corpus. Their
``:Clause``/``:Restriction`` nodes are unscoped themselves — scope is read by joining up to
``:Document`` (``IN_DOCUMENT`` / ``DERIVED_FROM``), the same way ``doc_type``/``corpus`` filters
already work in ``src/graph/reader.py``. ``:Entity``/``:RestrictionKind`` are never scoped — user
restrictions resolve into the same shared vocabulary as the official corpus, which is what lets
``SHARES_ENTITY`` (and, on top of it, ``CONFLICTS_WITH``) bridge a user restriction to an official
one.
"""

from __future__ import annotations

import structlog

from src.common.config import Settings
from src.graph.client import Neo4jClient

log = structlog.get_logger(__name__)


class VectorIndexDimensionMismatch(RuntimeError):
    """Existing Neo4j vector indexes use a different embedding space."""


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

# Non-unique index for scoped lookups/deletes (user document indices).
INDEXES: list[str] = [
    "CREATE INDEX document_scope IF NOT EXISTS "
    "FOR (d:Document) ON (d.user_id, d.scenario_id)",
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
    return [*CONSTRAINTS, *INDEXES, *vector_index_statements(settings)]


def _vector_index_names(settings: Settings) -> set[str]:
    return {
        settings.restriction_vector_index,
        settings.clause_vector_index,
        settings.entity_vector_index,
        settings.kind_vector_index,
    }


async def _assert_vector_dimensions(client: Neo4jClient, settings: Settings) -> None:
    rows = await client.run(
        "SHOW VECTOR INDEXES YIELD name, options RETURN name, options"
    )
    expected = int(settings.vector_size)
    configured = _vector_index_names(settings)
    actual: dict[str, int | None] = {}
    for row in rows:
        name = row.get("name")
        if name not in configured:
            continue
        options = row.get("options") or {}
        index_config = options.get("indexConfig") or {}
        actual[name] = index_config.get("vector.dimensions")

    mismatches = {
        name: actual.get(name)
        for name in sorted(configured)
        if actual.get(name) != expected
    }
    if mismatches:
        details = ", ".join(
            f"{name}={dimension if dimension is not None else 'missing'}"
            for name, dimension in mismatches.items()
        )
        raise VectorIndexDimensionMismatch(
            "Neo4j vector indexes do not match "
            f"NG_VECTOR_SIZE: expected {expected}, found {details}. "
            "Rebuild the stored embeddings and vector indexes together, or start "
            "NormGraph with a fresh Neo4j data volume."
        )


async def ensure_schema(client: Neo4jClient, settings: Settings) -> None:
    """Create the schema and reject an incompatible persisted vector space."""
    for stmt in schema_statements(settings):
        await client.run(stmt)
    await _assert_vector_dimensions(client, settings)
    log.info("graph_schema_ensured", vector_dim=settings.vector_size)

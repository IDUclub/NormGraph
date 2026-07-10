# Architecture

NormGraph is a FastAPI + FastMCP service that turns IDU_DVD's document corpus into a **graph of
normative restrictions** stored in Neo4j, and serves it over HTTP and MCP.

## Components

```text
                 ┌──────────────────────────── NormGraph ────────────────────────────┐
                 │                                                                    │
 IDU_DVD ───────▶│  dvd_client ──▶ ingestion ──▶ graph.writer ─┐                      │
 /library,/search│                                             ├──▶  Neo4j            │
 document.events▶│  sync.consumer ──▶ sync.service ──▶ pipeline ┘   (graph + vectors) │
 (Kafka)         │                         │                                          │
                 │  providers (LLM+embed) ◀┘                          graph.reader ──▶│──▶ query ──▶ REST + MCP
                 └────────────────────────────────────────────────────────────────────┘
     LLM (OpenAI-compatible / Ollama)          embeddings (Giga-Embeddings-instruct, 2048-d)
```

| Package | Responsibility |
|---|---|
| `src/dvd_client` | Async client for IDU_DVD (`/library`, `/search`, lookup) + DTOs. |
| `src/graph` | Neo4j client, schema (constraints + vector indexes), `writer` (idempotent writes), `reader` (search/traversal/facets). |
| `src/ingestion` | Structural layer: pull a document, upsert `:Document` / `:Clause`, build `PART_OF` and `REFERENCES`. |
| `src/pipeline` | Restriction extraction: langextract prompt/examples, extractor, kind vocabulary, entity resolver, orchestration. |
| `src/providers` | Provider-agnostic LLM + embeddings interfaces, OpenAI-compatible & Ollama implementations, langextract backend. |
| `src/query` | Read orchestration: embed query → graph read → response DTOs (search, applicable, get, graph, facets). |
| `src/sync` | Lifecycle: Kafka consumer (otteroad), sync service (ingest+extract / delete / reconcile), idempotency guard. |
| `src/dto` | API request/response models for the query surface. |
| `src/mcp_server` | FastMCP server mounted at `/mcp`, mirroring the query API. |
| `src/system_service` | Health, logs, effective settings. |

Everything is wired once in `src/dependencies.py` (the composition root) and shared through the
FastAPI lifespan.

## Graph model

### Nodes

| Label | Key | Notable properties |
|---|---|---|
| `:Document` | `doc_id` | `name`, `version`, `version_id`, `content_hash`, `doc_type`, `corpus`, `lang` |
| `:Clause` | `node_id` | `doc_id`, `version`, `version_id`, `numbering`, `breadcrumb`, `type`, `depth`, `order`, `char_start/end`, `span_id`, `tags`, `text`, `embedding` |
| `:Restriction` | `id` | `subject`, `object`, `kind`, `kind_status`, `value_operator/number/unit/condition`, `doc_id`, `version_id`, `clause_node_id`, `extraction_text`, `char_start/end`, `embedding` |
| `:Entity` | `normalized` | `name`, `aliases`, `status`, `embedding` |
| `:RestrictionKind` | `name` | `status` (`approved`/`pending`), `aliases`, `embedding` |
| `:PendingReference` | `key` | `target_name`, `target_numbering` (a referenced clause/doc not yet in the store) |

### Edges

| Edge | From → To | Meaning |
|---|---|---|
| `IN_DOCUMENT` | Clause → Document | clause membership |
| `PART_OF` | Clause → Clause | structural hierarchy (parent) |
| `REFERENCES` | Clause → Clause / Document / PendingReference | cross-reference (props: `scope`, `resolved`, `raw`, `target_numbering`) |
| `DERIVED_FROM` | Restriction → Clause | provenance: which clause a restriction came from |
| `HAS_SUBJECT` | Restriction → Entity | the entity that imposes the restriction |
| `APPLIES_TO` | Restriction → Entity | the entity the restriction applies to |
| `OF_KIND` | Restriction → RestrictionKind | controlled-vocabulary kind |
| `SHARES_ENTITY` | Restriction — Restriction | undirected: two restrictions share a subject/object entity |

### The restriction graph

Two restrictions are "related" when:

1. **`SHARES_ENTITY`** — they share a subject or object entity (semantic link); or
2. **reference** — their clauses are connected by the documents' `REFERENCES` graph
   (`Restriction → Clause -[:REFERENCES]- Clause ← Restriction`).

`neighbors`/`traverse` expand both. Because entities are deduplicated across documents (see
[pipeline](pipeline.md)), the same entity written differently in different documents collapses onto
one `:Entity`, which is what connects restrictions from different documents.

### Vector indexes

Four native Neo4j vector indexes (cosine, dimension = `NG_VECTOR_SIZE`, default 2048) are
provisioned at startup: `restriction_embedding`, `clause_embedding`, `entity_embedding`,
`kind_embedding`. They power semantic search over restrictions and the embedding-similarity
matching used for entity dedup and kind resolution.

## Reuse from IDU_DVD

IDU_DVD already parses documents into clauses with hierarchy, tags, source grounding
(`char_start/end`, `span_id`) and **resolved references** (`target_doc_id`, `target_node_id`,
`resolved`). NormGraph consumes those via the `/library/documents/{doc_id}` read API (which surfaces
`references` — a small IDU_DVD DTO extension NormGraph relies on) and builds its `REFERENCES` graph
directly, without re-parsing. When the graph lacks coverage for a text query, search can fall back
to IDU_DVD `/search` for grounded source snippets.

## Runtime & conventions

- FastAPI app on port **8020**; MCP mounted at `/mcp`; Swagger at `/docs`.
- Python 3.13+, managed with **uv**; `black` + `isort` via pre-commit; structlog JSON logging.
- Joins the Docker **`localnet`**; Neo4j runs via `docker compose`.
- No authentication yet — keep on a trusted network.

# API

Base URL `http://localhost:8020`. Interactive docs (Swagger) at `/docs`; MCP at `/mcp`. All models
are pydantic; request/response DTOs live in `src/dto/query.py`. The API is unauthenticated — keep it
on a trusted network.

## Endpoint list

| Method & path | Purpose |
|---|---|
| `POST /restrictions/search` | search restrictions by text and/or filters |
| `POST /restrictions/applicable` | restrictions applying to a given object/entity |
| `GET /restrictions/{id}` | one restriction + provenance + direct neighbours |
| `GET /restrictions/{id}/graph` | traverse the restriction graph |
| `GET /entities` | canonical entities (facets) |
| `GET /restriction-kinds` | restriction-kind vocabulary |
| `POST /ingestion/documents/{doc_id}` | structural ingest of one document |
| `POST /ingestion/by-name` | structural ingest by document name |
| `GET /ingestion/stats` | node/edge counts |
| `POST /extraction/documents/{doc_id}` | extract restrictions from an ingested document |
| `POST /sync/documents/{doc_id}` | ingest + extract one document (idempotent) |
| `POST /sync/by-name` | ingest + extract by name |
| `POST /sync/reconcile` | force a full catch-up reconcile |
| `DELETE /sync/by-name` | remove a document (all versions) from the graph |
| `GET /sync/status` | Kafka consumer + sync settings |
| `GET /system/health` | readiness (pings Neo4j) |
| `GET /system/settings` | effective `NG_` config (secrets masked) |
| `GET /system/logs` | download the JSON log file |
| `GET /ping` | liveness |

## Common shapes

`RestrictionOut`:

```json
{
  "id": "eef6e173b5...",
  "subject": "санитарно-защитная зона",
  "object": "полоса древесно-кустарниковых насаждений",
  "kind": "минимальная_ширина",
  "kind_status": "approved",
  "value": {"operator": ">=", "number": 50, "unit": "м", "condition": null},
  "extraction_text": "полосу ... шириной не менее 50 м",
  "score": 0.66,
  "subject_normalized": "санитарно-защитная зона",
  "object_normalized": "полоса древесно-кустарниковых насаждений",
  "tags": ["зонирование"],
  "provenance": {
    "doc_id": "1d09...", "name": "СП 42.13330.2016", "version": "2016",
    "version_id": "v1", "doc_type": "regulation", "corpus": "norms", "lang": "ru",
    "clause_node_id": "a1b2...", "numbering": "8.6", "breadcrumb": "СП / 8 / 8.6",
    "char_start": 1234, "char_end": 1300
  }
}
```

`value` is `null` when the restriction has no quantitative constraint. `score` is filled only for
vector (text-query) search.

## POST /restrictions/search

Search restrictions. Body (`RestrictionSearchRequest`):

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | str? | null | free-text query; when omitted → filtered listing (no vector) |
| `kind` | str? | null | filter by restriction kind |
| `doc_id` | str? | null | filter by document |
| `document_names` | list[str]? | null | filter by any of these document names |
| `version` | str? | null | filter by version or `version_id` |
| `doc_type` / `corpus` / `lang` | str? | null | document classification filters |
| `tags` | list[str]? | null | filter by clause tags (any of) |
| `subject` / `object` | str? | null | match the subject/object entity (normalized/alias) |
| `limit` | int | 10 | max hits |
| `neighbors_depth` | int | 0 | also return the graph neighbourhood up to this depth |

Response (`SearchResponse`): `{ count, hits: [RestrictionOut], neighbors: [{relation, restriction}], dvd_fallback: [DVDHit] }`.
`dvd_fallback` is filled only when a text query returns no restrictions and `NG_DVD_SEARCH_FALLBACK`
is on — it carries raw IDU_DVD source snippets.

```bash
curl -X POST http://localhost:8020/restrictions/search \
     -H "Content-Type: application/json" \
     -d '{"query": "санитарно-защитная зона", "tags": ["зонирование"], "limit": 5}'
```

## POST /restrictions/applicable

Which restrictions apply to a given object/entity (compliance-style). Body (`ApplicableRequest`):
same filters as search, plus a required `object` (the entity to check), optional `subject`, `limit`
(default 20). The object is resolved to canonical entities (exact + embedding-nearest ≥
`NG_ENTITY_MERGE_THRESHOLD`), and restrictions `APPLIES_TO` those entities are returned. Response is
a `SearchResponse`.

```bash
curl -X POST http://localhost:8020/restrictions/applicable \
     -H "Content-Type: application/json" -d '{"object": "жилая застройка", "limit": 10}'
```

## GET /restrictions/{id}

One restriction as `RestrictionDetail` = `RestrictionOut` + `neighbors: [{relation, restriction}]`
(direct neighbours). `relation` ∈ `shares_entity` | `reference`. `404` if not found.

## GET /restrictions/{id}/graph?depth=N

Traverse the restriction graph from a restriction up to `depth` hops (capped by
`NG_MAX_TRAVERSAL_DEPTH`). Response (`GraphResponse`):

```json
{
  "root_id": "r1", "depth": 2,
  "nodes": [ RestrictionOut, ... ],
  "edges": [ {"source": "r1", "target": "r2", "relation": "shares_entity"}, ... ]
}
```

## GET /entities  ·  GET /restriction-kinds

Facets. `GET /entities?query=<substr>&limit=<n>` → `[{normalized, name, aliases, status,
restriction_count}]`, most-referenced first. `GET /restriction-kinds` → `[{name, status, aliases,
restriction_count}]` including auto-added `pending` kinds.

## Ingestion & extraction

- `POST /ingestion/documents/{doc_id}` → `IngestResult` `{doc_id, clauses, references,
  pending_references, pruned_clauses, content_hash, skipped, reason}`. Structural only (no LLM).
- `POST /ingestion/by-name?name=<name>` → `[IngestResult]`.
- `GET /ingestion/stats` → `{documents, clauses, references, pending_references, restrictions}`.
- `POST /extraction/documents/{doc_id}` → `ExtractResult` `{doc_id, clauses_processed, restrictions,
  pending_kinds, replaced, skipped, reason}`. Needs the LLM + embedder.

## Sync

- `POST /sync/documents/{doc_id}?replace=false` → `SyncResult` `{doc_id, name, clauses, restrictions,
  pruned_clauses, replaced, extraction_skipped, skipped, reason}`. Ingest **and** extract, with the
  idempotency guard (`extraction_skipped=true` when unchanged and already extracted). `404` if the
  document is not in DVD.
- `POST /sync/by-name?name=<name>&replace=false` → `[SyncResult]`.
- `POST /sync/reconcile` → `ReconcileResult` `{added, updated, deleted, unchanged, failed, skipped,
  reason}`.
- `DELETE /sync/by-name?name=<name>` → `DeleteResult` `{name, documents_deleted, clauses_deleted,
  restrictions_deleted, doc_ids}`.
- `GET /sync/status` → `{kafka_enabled, kafka_topic, kafka_group_id, kafka_bootstrap_servers,
  reconcile_on_startup}`.

## System

- `GET /system/health` → `{status, graph}` (pings Neo4j).
- `GET /system/settings` → effective `NG_` configuration; secrets (`neo4j_password`, `llm_api_key`,
  `embeddings_api_key`) masked as `***`.
- `GET /system/logs` → the JSON log file.

## MCP tools (`/mcp`)

The FastMCP server mirrors the query API so gMART can reach restrictions over MCP.

| Tool | Description |
|---|---|
| `search_restrictions` | text/filter search; params mirror `POST /restrictions/search` |
| `restrictions_applicable` | restrictions applying to an `object` (+ optional filters) |
| `get_restriction` | one restriction + provenance + neighbours |
| `traverse_restrictions` | graph traversal from a restriction (`depth`) |
| `list_entities` | entity facets |
| `list_restriction_kinds` | kind vocabulary |
| `health` | liveness of the MCP server |

Example (FastMCP in-memory client):

```python
from fastmcp import Client
from src.mcp_server.server import mcp

async with Client(mcp) as client:
    res = await client.call_tool("search_restrictions", {"kind": "запрет_размещения", "limit": 5})
    print(res.structured_content)
```

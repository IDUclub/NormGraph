# API

Base URL `http://localhost:8020`. Interactive docs (Swagger) at `/docs`; MCP at `/mcp`. All models
are pydantic; request/response DTOs live in `src/dto/query.py`. Functional HTTP and MCP endpoints
require a bearer service token. User-scoped operations additionally require `X-User-Id`.

## Endpoint list

| Method & path | Purpose |
|---|---|
| `POST /restrictions/search` | search restrictions by text and/or filters |
| `POST /restrictions/applicable` | restrictions applying to a given object/entity |
| `POST /restrictions/list` | complete keyset-paged listing for audits |
| `GET /restrictions/{id}` | one restriction + provenance + direct neighbours |
| `GET /restrictions/{id}/graph` | traverse the restriction graph |
| `GET /check-plans/review` | list auto/pending plans for expert review |
| `POST /check-plans/backfill` | generate a bounded page of missing plans without re-extraction |
| `POST /check-plans/{id}/regenerate` | preview or regenerate one stored plan with revision protection |
| `GET /check-plans/{id}/revisions` | immutable CheckPlan revision history |
| `POST /check-plans/{id}/review` | approve, reject or replace a plan |
| `GET /entities` | canonical entities (facets) |
| `POST /entities/resolve` | candidate canonical entities for free-text topics |
| `POST /documents/list` | documents holding matching restrictions, with executable counts |
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
| `kinds` | list[str]? | null | any of these kinds (e.g. all placement kinds) |
| `doc_id` | str? | null | filter by document |
| `document_names` | list[str]? | null | filter by any of these document names |
| `version` | str? | null | filter by version or `version_id` |
| `doc_type` / `corpus` / `lang` | str? | null | document classification filters |
| `tags` | list[str]? | null | filter by clause tags (any of) |
| `subject` / `object` | str? | null | match the subject/object entity (normalized/alias) |
| `entities` | list[str]? | null | topic filter: any of these entities (normalized/alias) as the subject, the object or a declared layer of the current CheckPlan |
| `limit` | int | 10 | max hits, 1–500 |
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
(default 20, at most 500). The object is resolved to canonical entities (exact + embedding-nearest ≥
`NG_ENTITY_QUERY_THRESHOLD`, looser than the merge threshold), and restrictions `APPLIES_TO` those entities are returned. Response is
a `SearchResponse`.

```bash
curl -X POST http://localhost:8020/restrictions/applicable \
     -H "Content-Type: application/json" -d '{"object": "жилая застройка", "limit": 10}'
```

## POST /restrictions/list

Complete listing for audits (the gMART compliance check reads the whole corpus this way). One
response is at most 500 restrictions: larger windows exhaust the server's memory, so search and
applicable reject them too. Body (`RestrictionListRequest`): the search filters plus `after_id`
(null for the first page), `limit` (default 200, 1–500) and `executable_only` (only restrictions whose
current CheckPlan is `auto` or `reviewed`). Pages are ordered by restriction id, so documents ingested
while a client pages cannot shift or duplicate rows. Response (`RestrictionPage`):
`{ count, hits: [RestrictionOut], next_after_id }`; repeat with `after_id = next_after_id` until it is
null.

```bash
curl -X POST http://localhost:8020/restrictions/list \
     -H "Content-Type: application/json" -d '{"limit": 200, "executable_only": true}'
```

### Topic filter (`entities`)

`entities` is shared by search, applicable, list and `POST /documents/list`. Each value is
normalized and expanded to the canonical key and every alias of the entities it names, so
`["школы"]` matches the entity `школа`. A restriction passes when its subject or object is one of
them, **or** when a declared layer of its current CheckPlan is (a plan may name an entity that
is neither — e.g. the zones of an area-ratio rule, or an expert replacement). Layer labels are
stored normalized on the plan (`layer_entities`); plans saved before that field existed are keyed
once at startup. Use `POST /entities/resolve` to turn user wording into canonical names first.

## POST /entities/resolve

Candidate canonical entities for free-text topics — for a caller (the gMART compliance agent)
that lets the user or an LLM pick which entities a topic means. Body (`EntityResolveRequest`):
`terms` (1–10 strings) and `limit` (candidates per term, default 10, at most 50). For each term:
entities whose normalized name or alias equals it, or whose name contains every crude word stem
(`школы` → `школ`), then the embedding-nearest entities. Vector matches are **not** cut at
`NG_ENTITY_QUERY_THRESHOLD`; their `score` is returned instead. Response:
`[{term, candidates: [{normalized, name, aliases, status, restriction_count, executable_count,
match, score}]}]`, `match` ∈ `exact` | `alias` | `text` | `vector`. Counts cover restrictions
naming the entity as subject or object; `executable_count` those with an `auto`/`reviewed` plan.
If the embedding service fails, the text matches are still returned.

```bash
curl -X POST http://localhost:8020/entities/resolve \
     -H "Content-Type: application/json" -d '{"terms": ["школы"], "limit": 10}'
```

## POST /documents/list

Documents whose restrictions match the filters, e.g. to offer the user a choice of documents.
Body (`DocumentListRequest`): the search filters (including `entities`), `executable_only`
(keep documents with at least one restriction whose current plan is `auto`/`reviewed`) and
`limit` (default 200, 1–500). Documents of user indices (`user_id` set) are always excluded:
the listing carries no user scope to limit them to their owner. Response
(`DocumentListResponse`): `{ count, documents: [{doc_id, name, version, version_id, doc_type,
corpus, restriction_count, executable_count}] }`, ordered by `executable_count`.

```bash
curl -X POST http://localhost:8020/documents/list \
     -H "Content-Type: application/json" \
     -d '{"entities": ["школа"], "executable_only": true}'
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

## POST /check-plans/backfill

Generate plans directly from stored restrictions that have no current `CheckPlan`. The operation is
non-destructive, uses keyset pagination, and does not run clause extraction again.

```json
{"limit": 100, "after_id": null, "dry_run": false}
```

The response reports `selected`, `generated`, `auto`, `unsupported`, `skipped`, `failed`, individual
`failures`, and the pagination fields `has_more`/`next_after_id`. Pass `next_after_id` as the next
request's `after_id` while `has_more=true`. A dry run only reads the page. Re-running from
`after_id=null` is safe and retries rows that previously failed; restrictions with a current plan are
skipped atomically.

## POST /check-plans/{id}/regenerate

Rebuild a plan from the stored restriction using the current planner. Requires a service bearer
token. Fetch the current revision from `GET /check-plans/{id}/revisions` first (use `0` when no
plan exists). Preview is the default and runs the planner without writing:

```json
{"expected_revision": 1, "dry_run": true}
```

The response contains `restriction_id`, `revision`, `dry_run`, and `plan`. Set `dry_run=false`
with the same expected revision to append a new current revision; previous revisions remain in
history. The planner runs again on save, so an LLM-generated preview may differ from the saved plan.
The response's revision is the existing revision for preview and the newly created revision for save.
`404` means the restriction was not found; `409` means the revision changed or an expert decision
is protected. Plans marked `reviewed` or carrying an expert author (including rejected plans) cannot
be regenerated. Invalid request bodies return `422`.

### Accessibility and applicability limits

For the residential educational-accessibility case, the planner maps the checked layer to
`Жилой дом` and uses separate mandatory service layers `Школа` and `Детский сад` when both are named.
Kilometer distances are converted to meters. Stored extraction text and applicability conditions
are retained.

When the quotation specifies only distance, without an explicit walking/transport accessibility
or route requirement and without additional conditions, the planner produces an executable
`presence_within` plan with status `auto`. For example, the base "at most 500 m" restriction uses
geometric distance. Mentioning a school or kindergarten alone does not imply a walking route.

The current v1 executor uses geometric buffers and cannot establish walking routes or applicability
conditions. Such plans therefore have root `template=unsupported` and `planner_status=unsupported`:
they must yield an unverified/unknown result, never a compliance verdict from a straight-line radius.
`params.blocked_reasons` explains the missing capabilities (`walking_route_required` and/or
`applicability_not_verified`); `params.condition` retains the condition. When possible,
`params.candidate_plan` contains a corrected geometric draft **for inspection only**. It must not be
executed separately or approved without resolving these limitations. A rural 1 km draft does not
establish that the rural limit applies to an urban scenario.

These guards cover explicit walking/transport accessibility or route wording, and nonempty
extracted conditions; they are not a general semantic validator for arbitrary extracted norms.
Existing stored plans are unchanged until explicitly regenerated after deployment. Missing-plan
backfill does not repair existing plans.

## Ingestion & extraction

- `POST /ingestion/documents/{doc_id}` → `IngestResult` `{doc_id, clauses, references,
  pending_references, pruned_clauses, content_hash, skipped, reason}`. Structural only (no LLM).
- `POST /ingestion/by-name?name=<name>` → `[IngestResult]`.
- `GET /ingestion/stats` → `{documents, clauses, references, pending_references, restrictions}`.
- `POST /extraction/documents/{doc_id}` → `ExtractResult` `{doc_id, clauses_processed, restrictions,
  pending_kinds, conflicts, replaced, skipped, reason, warnings, incomplete, failed_clause_ids}`. Needs the LLM + embedder.

### POST /extraction/backfill

Recover extraction for already-ingested documents with **zero restrictions**, using stored clauses
without downloading the documents again. Normal extraction also generates CheckPlans.
Requires the same service bearer token as other extraction endpoints.

```json
{"limit": 1, "after_id": null, "dry_run": true}
```

`limit` is the number of documents (1–20, default 1). `dry_run=true` lists candidate document IDs
without LLM calls or graph writes; set it to `false` to run extraction. The request waits for the
page to finish; documents run sequentially, with clause concurrency controlled by
`NG_EXTRACT_CONCURRENCY`. Allow enough HTTP timeout for full-document extraction.

The response includes `selected`, `extracted`, `skipped`, `failed`, `restrictions`, per-document
`items` (status, clause/restriction counts and reason), `has_more`, `next_after_id`, and `dry_run`.
When `has_more=true`, pass `next_after_id` as the next request's `after_id`. A failed document does
not stop the page. A successfully processed document can legitimately produce zero restrictions;
the cursor advances past it, but a new scan can select it again.

Documents that already have restrictions are excluded and existing restrictions are not deleted.
This endpoint does not repair partially extracted documents: use
`POST /extraction/documents/{doc_id}` for those, including a failed run that already wrote some
restrictions. Restarting a scan without `after_id` retries documents that still have zero
restrictions. Avoid overlapping extraction/sync runs for the same documents; the state recheck is
best-effort, not a distributed lock.

## Sync

- `POST /sync/documents/{doc_id}?replace=false` → `SyncResult` `{doc_id, name, clauses, restrictions,
  pruned_clauses, replaced, extraction_skipped, skipped, reason, extraction_incomplete, warnings, failed_clause_ids}`. Ingest **and** extract, with the
  idempotency guard (`extraction_skipped=true` when unchanged and already extracted). `404` if the
  document is not in DVD.
- `POST /sync/by-name?name=<name>&replace=false` → `[SyncResult]`.
- `POST /sync/reconcile` → `ReconcileResult` `{added, updated, relabelled, deleted, unchanged, failed,
  skipped, reason}`.
- `DELETE /sync/by-name?name=<name>` → `DeleteResult` `{name, documents_deleted, clauses_deleted,
  restrictions_deleted, doc_ids}`.
- `GET /sync/status` → `{kafka_enabled, kafka_topic, kafka_group_id, kafka_bootstrap_servers,
  reconcile_on_startup}`.

## System

`GET /system/logs` and `GET /system/settings` require no authorization. `GET /system/health` requires a service token.

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
| `list_restrictions` | complete keyset-paged listing; params mirror `POST /restrictions/list` |
| `resolve_entities` | candidate canonical entities for free-text topics; mirrors `POST /entities/resolve` |
| `list_restriction_documents` | documents with total/executable counts; mirrors `POST /documents/list` |
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

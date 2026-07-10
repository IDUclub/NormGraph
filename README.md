# NormGraph

**Graph-RAG of normative restrictions** built on top of
[IDU_DVD](https://github.com/IDUclub/IDU_DVD).

NormGraph turns a corpus of Russian normative documents (СП/СНиП/ГОСТ/СанПиН, ingested and vectorized
by IDU_DVD) into a **queryable graph of restrictions**. For every clause it extracts restriction
triples — **`{subject, object, kind}` + an optional structured `value`** — with an LLM (via
[google/langextract](https://github.com/google/langextract)), links them to each other (through
shared entities and the documents' cross-references), and stores everything in **Neo4j** with native
vector indexes. It then serves the restrictions over **HTTP and MCP**: by free-text query, by
structured filters, and by graph-neighbourhood traversal.

Part of the ICII urban-planning platform; the primary consumer is the **gMART** orchestrator
(a compliance-checking agent there is a separate, later task).

## What it does

- **Ingests** documents from IDU_DVD's `/library` API — clauses, hierarchy, source grounding, and
  the already-resolved cross-document **references**.
- **Extracts** restrictions per clause with langextract, e.g.
  `санитарно-защитная зона → полоса насаждений | минимальная_ширина | ≥ 50 м`.
- **Resolves** each triple's *kind* against a controlled, dynamically-extensible vocabulary and its
  *subject/object* against canonical entities (deduped across documents by name + embedding
  similarity), so the same entity written differently collapses onto one node — that is what makes
  the restriction graph connect.
- **Links** restrictions via `SHARES_ENTITY` (shared subject/object) and through the documents'
  `REFERENCES` graph.
- **Serves** everything: vector search + filters + graph traversal, over REST and MCP.
- **Stays in sync** with IDU_DVD via Kafka lifecycle events (`document.events`) and a startup
  reconcile, with an idempotency guard so re-processing an unchanged document is cheap.

## Restriction model

| Slot | Meaning | Example |
|---|---|---|
| `subject` | the entity that imposes the restriction (verbatim from the text) | `санитарно-защитная зона` |
| `object` | what the restriction applies to (free text) | `объекты пищевой промышленности` |
| `kind` | kind of restriction, from a controlled vocabulary (extensible) | `запрет_размещения` |
| `value` | optional quantitative constraint `{operator, number, unit, condition}` | `{">=", 50, "м", null}` |

A clause with conditional norms yields several restrictions (one per value).

## Architecture

```text
IDU_DVD ──/library, /search──▶ NormGraph ingest ──▶ :Document / :Clause / REFERENCES / PART_OF
IDU_DVD ──document.events (Kafka)──▶ sync consumer ──▶ (re)ingest + (re)extract
                                             │
                        langextract + LLM ◀──┤ extract restrictions per clause
                        embeddings (giga)  ◀─┤ vector every restriction / entity / kind
                                             ▼
                                          Neo4j graph (vector-indexed)
                                             ▲
        gMART / clients ──REST + MCP──▶ query: search · applicable · get · graph · facets
```

Graph model: nodes `:Document`, `:Clause`, `:Restriction`, `:Entity`, `:RestrictionKind`,
`:PendingReference`; edges `IN_DOCUMENT`, `PART_OF`, `REFERENCES`, `DERIVED_FROM`, `HAS_SUBJECT`,
`APPLIES_TO`, `OF_KIND`, `SHARES_ENTITY`. See [docs/en/architecture.md](docs/en/architecture.md).

## Provider-agnostic

Both the LLM and the vectorizer sit behind small abstract interfaces
([`src/providers`](src/providers)). A single setting points NormGraph at any **OpenAI-compatible**
endpoint (vLLM, LM Studio, llama.cpp, Ollama's `/v1`) or native **Ollama**. Defaults match the IDU
contour: an OpenAI-compatible chat model and **Giga-Embeddings-instruct (2048-d)**.

## Quick start

```bash
docker network create localnet          # once, shared across ICII services
make install                            # uv sync (incl. dev group)
make up                                 # start Neo4j (docker compose)
cp .env.example .env                    # point at your LLM / embeddings / IDU_DVD / Kafka
make run                                # uvicorn on :8020 — Swagger at /docs, MCP at /mcp
make test                               # hermetic unit tests
```

Then populate the graph and query it:

```bash
# ingest + extract one document (structural ingest needs no LLM; extraction does)
curl -X POST "http://localhost:8020/sync/documents/<doc_id>"

# search restrictions
curl -X POST http://localhost:8020/restrictions/search \
     -H "Content-Type: application/json" \
     -d '{"query": "ограничения в санитарно-защитной зоне", "limit": 5}'
```

## HTTP endpoints (summary)

| Method & path | Purpose |
|---|---|
| `POST /restrictions/search` | text and/or filtered restriction search (+ optional neighbourhood) |
| `POST /restrictions/applicable` | restrictions applying to a given object/entity (compliance) |
| `GET /restrictions/{id}` | one restriction + provenance + direct neighbours |
| `GET /restrictions/{id}/graph?depth=` | traverse the restriction graph |
| `GET /entities`, `GET /restriction-kinds` | facets / vocabularies |
| `POST /ingestion/documents/{doc_id}` · `POST /ingestion/by-name` · `GET /ingestion/stats` | structural ingest |
| `POST /extraction/documents/{doc_id}` | run extraction over an ingested document |
| `POST /sync/documents/{doc_id}` · `POST /sync/by-name` · `POST /sync/reconcile` · `GET /sync/status` · `DELETE /sync/by-name` | lifecycle sync |
| `GET /system/health` · `GET /system/settings` · `GET /system/logs` · `GET /ping` | operations |
| `/mcp` | MCP server (same query tools) |

MCP tools: `search_restrictions`, `restrictions_applicable`, `get_restriction`,
`traverse_restrictions`, `list_entities`, `list_restriction_kinds`, `health`.

Full reference: [docs/en/api.md](docs/en/api.md).

## Configuration

All settings live in [`src/common/config/app_config.py`](src/common/config/app_config.py)
(pydantic-settings), overridable via **`NG_`**-prefixed environment variables or `.env`. See
[`.env.example`](.env.example) (network wiring), [`.env.full.example`](.env.full.example) (full
reference) and [docs/en/configuration.md](docs/en/configuration.md). Defaults target the IDU
contour, so the app starts without a `.env`.

## Documentation

| | English | Русский |
|---|---|---|
| Architecture & graph model | [docs/en/architecture.md](docs/en/architecture.md) | [docs/ru/architecture.md](docs/ru/architecture.md) |
| Pipeline (ingest · extract · sync) | [docs/en/pipeline.md](docs/en/pipeline.md) | [docs/ru/pipeline.md](docs/ru/pipeline.md) |
| HTTP & MCP API | [docs/en/api.md](docs/en/api.md) | [docs/ru/api.md](docs/ru/api.md) |
| Configuration | [docs/en/configuration.md](docs/en/configuration.md) | [docs/ru/configuration.md](docs/ru/configuration.md) |

## Testing

```bash
make test              # unit tests (hermetic — mock every external boundary)
make test-integration  # integration tests (need live Neo4j; self-skip otherwise)
make test-all          # everything
```

Unit tests are hermetic; integration tests self-skip when Neo4j / IDU_DVD / the LLM is unavailable.

## Conventions

FastAPI + FastMCP, **uv**, **Python 3.13+**, `black` + `isort --profile black` (pre-commit),
structlog JSON logging, Docker `localnet`, port **8020**. No authentication yet — keep on a trusted
network. Part of the ICII workspace; commit inside this submodule to the IDUclub remote.

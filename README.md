# NormGraph

Graph-RAG of **normative restrictions** built on top of [IDU_DVD](https://github.com/IDUclub/IDU_DVD).

NormGraph pulls document clauses and their cross-references from IDU_DVD, extracts restriction
triples — **`{subject, object, kind}` + optional structured `value`** — from each clause with an
LLM (via [google/langextract](https://github.com/google/langextract)), and stores them as a graph
in **Neo4j** (with a native vector index). It then serves the restrictions over **HTTP and MCP**:
by plain text query and by structured filters (tags, document names, versions, kind, entities),
with graph-neighbourhood traversal.

Part of the ICII urban-planning platform; the primary consumer is the gMART orchestrator.

## Status

Early scaffold. Implemented so far:

- provider-agnostic **LLM** and **embeddings** layers (default: OpenAI-compatible chat +
  Giga-Embeddings-instruct 2048-d; native Ollama alternative);
- **Neo4j** async client;
- FastAPI app with the **MCP** server mounted at `/mcp`, `/system/*` operational endpoints and
  `/ping`.

Graph schema, the IDU_DVD ingestion + langextract extraction pipeline, the restriction query API
and Kafka sync land in subsequent stages.

## Quick start

```bash
docker network create localnet   # once, shared across the ICII services
make install                     # uv sync (incl. dev group)
make up                          # start Neo4j (docker compose)
cp .env.example .env             # adjust endpoints if needed
make run                         # uvicorn on :8020 (Swagger at /docs, MCP at /mcp)
make test                        # hermetic unit tests
```

## Configuration

All settings live in `src/common/config/app_config.py` (pydantic-settings), overridable via
`NG_`-prefixed environment variables or `.env`. See `.env.example` (network endpoints) and
`.env.full.example` (full reference). Defaults target the IDU contour; the app starts without a
`.env`.

## Conventions

FastAPI + FastMCP, **uv**, **Python 3.13+**, `black` + `isort --profile black` (pre-commit),
structlog JSON logging, Docker `localnet`. No authentication yet — keep on a trusted network.

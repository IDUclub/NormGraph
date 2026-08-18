# Configuration

All settings live in `src/common/config/app_config.py` (pydantic-settings). Override any of them
with an environment variable using the **`NG_`** prefix, or via a `.env` file. Environment variables
take precedence over `.env`. Defaults target the IDU contour, so the app starts without any config.

- `.env.example` — minimal network wiring (the addresses you usually need to change).
- `.env.full.example` — every variable, for reference.

## Neo4j

| Variable | Default | Meaning |
|---|---|---|
| `NG_NEO4J_URI` | `bolt://localhost:7687` | Bolt URI |
| `NG_NEO4J_USER` | `neo4j` | user |
| `NG_NEO4J_PASSWORD` | `normgraph` | password (masked in `/system/settings`) |
| `NG_NEO4J_DATABASE` | `neo4j` | database |
| `NG_RESTRICTION_VECTOR_INDEX` | `restriction_embedding` | vector index name |
| `NG_CLAUSE_VECTOR_INDEX` | `clause_embedding` | vector index name |
| `NG_ENTITY_VECTOR_INDEX` | `entity_embedding` | vector index name |
| `NG_KIND_VECTOR_INDEX` | `kind_embedding` | vector index name |

## IDU_DVD

| Variable | Default | Meaning |
|---|---|---|
| `NG_DVD_BASE_URL` | `http://localhost:8100` | IDU_DVD base URL (prod publishes on 8100) |
| `NG_DVD_TIMEOUT` | `120.0` | HTTP timeout (s) |
| `NG_DVD_SEARCH_FALLBACK` | `true` | on empty graph results, fall back to IDU_DVD `/search` |

## LLM provider (restriction extraction)

| Variable | Default | Meaning |
|---|---|---|
| `NG_LLM_PROVIDER` | `openai_compatible` | `openai_compatible` or `ollama` |
| `NG_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible root (`…/v1`) |
| `NG_LLM_MODEL` | `qwen2.5:7b-instruct` | chat model id |
| `NG_LLM_API_KEY` | — | bearer token, if the endpoint needs one (masked) |
| `NG_LLM_TEMPERATURE` | `0.0` | sampling temperature |
| `NG_LLM_MAX_TOKENS` | `4096` | max output tokens |
| `NG_LLM_TIMEOUT` | `600.0` | HTTP timeout (s) |
| `NG_OLLAMA_BASE` | `http://localhost:11434` | native Ollama root (used when `NG_LLM_PROVIDER=ollama`) |

The default is OpenAI-compatible, so any of vLLM / LM Studio / llama.cpp / Ollama's `/v1` shim works
by pointing `NG_LLM_BASE_URL` at it. langextract runs through this provider.

## Embeddings provider (vectorizer)

| Variable | Default | Meaning |
|---|---|---|
| `NG_EMBEDDINGS_PROVIDER` | `openai_compatible` | `openai_compatible` (Giga) or `ollama` (e.g. bge-m3) |
| `NG_EMBEDDINGS_URL` | `http://localhost:8001` | embeddings service root (`POST /v1/embeddings`) |
| `NG_EMBEDDINGS_MODEL` | `ai-sage/Giga-Embeddings-instruct` | model id |
| `NG_EMBEDDINGS_API_KEY` | — | bearer token, if needed (masked) |
| `NG_EMBEDDINGS_QUERY_PROMPT` | Instruct prompt | query-side instruction (Giga is asymmetric) |
| `NG_EMBEDDINGS_TIMEOUT` | `600.0` | HTTP timeout (s) |
| `NG_VECTOR_SIZE` | `2048` | **must** match the model (giga = 2048, bge-m3 = 1024) and the vector indexes |
| `NG_EMBED_BATCH` | `32` | embedding batch size |

> Changing `NG_VECTOR_SIZE` requires recreating the vector indexes (drop them, or use a fresh Neo4j
> database), since a Neo4j vector index has a fixed dimension. Startup fails fast when a configured
> index already exists with another dimension; rebuild its stored embeddings and indexes together.

## Extraction pipeline

| Variable | Default | Meaning |
|---|---|---|
| `NG_EXTRACTION_PASSES` | `1` | langextract sequential passes per clause (recall vs cost) |
| `NG_ENTITY_MERGE_THRESHOLD` | `0.90` | cosine ≥ this merges an entity into an existing canonical |
| `NG_KIND_MATCH_THRESHOLD` | `0.88` | cosine ≥ this matches a kind; below → new `pending` kind |
| `NG_EXTRACT_CONCURRENCY` | `8` | max clauses processed concurrently through the LLM; graph writes remain ordered |

## Search / traversal

| Variable | Default | Meaning |
|---|---|---|
| `NG_SEARCH_LIMIT` | `10` | default result limit |
| `NG_MAX_TRAVERSAL_DEPTH` | `3` | cap on graph-neighbourhood expansion depth |

## Kafka sync

| Variable | Default | Meaning |
|---|---|---|
| `NG_KAFKA_BOOTSTRAP_SERVERS` | — (disabled) | broker(s); empty/unset = consumer off |
| `NG_KAFKA_SCHEMA_REGISTRY_URL` | contour registry | Avro Schema Registry |
| `NG_KAFKA_CLIENT_ID` | `normgraph` | client id |
| `NG_KAFKA_GROUP_ID` | `normgraph-sync` | consumer group (stable → offsets tracked per group) |
| `NG_KAFKA_TOPIC` | `document.events` | IDU_DVD lifecycle topic |
| `NG_KAFKA_AUTO_OFFSET_RESET` | `earliest` | first-run offset policy (see below) |
| `NG_RECONCILE_ON_STARTUP` | `true` | run a catch-up reconcile at startup |

**Offsets & "only unprocessed events".** With a stable `NG_KAFKA_GROUP_ID`, Kafka tracks the last
committed offset per group, so on restart the consumer resumes from it and processes only events it
hasn't handled yet (otteroad commits after a handler succeeds — at-least-once). `AUTO_OFFSET_RESET`
matters only on the very first start (no committed offset) or if offsets expire:

- `earliest` → consume the whole backlog once, then only new events (recommended if you want
  previously-unprocessed events, including those from before the consumer first ran);
- `latest` → skip the backlog, only new events from now on.

The idempotency guard (see [pipeline](pipeline.md)) makes replays cheap: an unchanged, already-
extracted document skips re-extraction.

## Logging

| Variable | Default | Meaning |
|---|---|---|
| `NG_LOG_DIR` | `./logs` | log directory |
| `NG_LOG_FILE` | `app.log` | JSON log file (served via `GET /system/logs`) |
| `NG_LOG_LEVEL` | `INFO` | log level |

## Example `.env` (IDU contour)

```dotenv
NG_DVD_BASE_URL=http://10.32.11.17:8100
NG_LLM_BASE_URL=http://a.dgx:11434/v1
NG_LLM_MODEL=gpt-oss:20b
NG_EMBEDDINGS_URL=http://a.dgx:8010
NG_KAFKA_BOOTSTRAP_SERVERS=10.32.1.65:9092,10.32.1.65:9093,10.32.1.65:9094
NG_KAFKA_SCHEMA_REGISTRY_URL=http://10.32.1.65:8081
NG_KAFKA_AUTO_OFFSET_RESET=earliest
```

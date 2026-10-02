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
| `NG_LLM_MAX_TOKENS` | `4096` | initial output-token window |
| `NG_LLM_MAX_TOKENS_LIMIT` | `16384` | window ceiling: an answer cut at the limit is requested again with a doubled window (4096 → 8192 → 16384). Short answers cost the same; long ones (a clause with many norms, a model that spent the budget reasoning) get finished. If a grown window does not fit the model context (vLLM answers 400), the answer already received is kept. Equal to `NG_LLM_MAX_TOKENS` disables the growth |
| `NG_LLM_TIMEOUT` | `600.0` | HTTP timeout (s) |
| `NG_LLM_REASONING_EFFORT` | — | `low` / `medium` / `high`: reasoning budget sent as `reasoning_effort` (gpt-oss on vLLM); empty = not sent. It drives restriction extraction: use `medium` (the gpt-oss default) for gpt-oss, `low` loses about half of the norms of a clause. An answer the model cut by reasoning is requested again with a grown window (`NG_LLM_MAX_TOKENS_LIMIT`) |
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
| `NG_ENTITY_QUERY_THRESHOLD` | `0.75` | cosine ≥ this resolves the `object` of an applicable query to a canonical entity (Giga: synonyms 0.77–0.87, unrelated facilities ≤ 0.68) |
| `NG_KIND_MATCH_THRESHOLD` | `0.88` | cosine ≥ this matches a kind; below → new `pending` kind |
| `NG_EXTRACT_CONCURRENCY` | `64` | max clauses processed concurrently through the LLM; graph writes remain ordered |

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
| `NG_CHECK_PLAN_BACKFILL_ON_STARTUP` | `true` | generate missing check plans in the background at every startup |
| `NG_RESTRICTION_REEMBED_ON_STARTUP` | `true` | recompute restriction vectors stored with an older embedding text (embeddings only, no LLM) |
| `NG_URBAN_API_URL` | — | Urban API root with the public type dictionaries (e.g. `http://host/api`); unset = no catalog grounding and no LLM rewrite |
| `NG_URBAN_CATALOG_TTL_SECONDS` | `3600` | refresh period of the cached dictionaries |
| `NG_CHECK_PLAN_REWRITE` | `true` | LLM rewrite pass for norms without a grounded plan |
| `NG_CHECK_PLAN_REWRITE_VOTES` | `3` | independent rewrites of a norm (temperatures 0, 0.3, 0.5) |
| `NG_CHECK_PLAN_REWRITE_AGREEMENT` | `2` | rewrites that must compile to the same plan |
| `NG_CHECK_PLAN_REASONING_EFFORT` | — | reasoning budget of the planner's LLM calls, independent of extraction; empty = `NG_LLM_REASONING_EFFORT` |
| `NG_CHECK_PLAN_TRANSPORT_SPEED_KMH` | `25` | average transport speed turning transport accessibility time into a radius |
| `NG_CHECK_PLAN_VERIFY` | `true` | LLM verifier that must confirm every automatic plan |
| `NG_CHECK_PLAN_MIN_DISTANCE_M` | `3.0` | smaller distances are in-building, not territorial |
| `NG_CHECK_PLAN_LLM_CONCURRENCY` | `16` | concurrent planner LLM requests |

Plan generation runs independently of reconcile: stored restrictions without a current
`CheckPlan` are processed in batches of 100 until the end, without document re-extraction.
Concurrency follows `NG_EXTRACT_CONCURRENCY`. Existing plans, including `unsupported`, are
skipped. Individual failures do not stop the pass; restrictions still missing plans are retried
at the next startup. Totals are logged as `check_plan_startup_completed`; database read failures
as `check_plan_startup_failed`. The API does not wait for the pass to finish.

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
NG_DVD_BASE_URL=http://localhost:8100
NG_LLM_BASE_URL=http://localhost:11434/v1
NG_LLM_MODEL=gpt-oss:20b
NG_EMBEDDINGS_URL=http://localhost:8010
NG_KAFKA_BOOTSTRAP_SERVERS=localhost:9092,localhost:9093,localhost:9094
NG_KAFKA_SCHEMA_REGISTRY_URL=http://localhost:8081
NG_KAFKA_AUTO_OFFSET_RESET=earliest
```

"""Application configuration (pydantic-settings).

Every value can be overridden via environment variables with the ``NG_`` prefix or through ``.env``.
The service builds a graph-RAG of normative restrictions on top of IDU_DVD: it pulls document
clauses + references, extracts restriction triples with an LLM (via langextract), and stores them
in Neo4j with a native vector index.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_OLLAMA_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama"}
)


def _url_host(url: str, variable: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{variable} must be an absolute http(s) URL")
    return parsed.hostname.rstrip(".").lower()


class Settings(BaseSettings):
    """Application configuration class: all tunable parameters in one place."""

    model_config = SettingsConfigDict(env_prefix="NG_", env_file=".env", extra="ignore")

    service_auth_server_url: str
    service_auth_realm: str
    service_auth_client_id: str
    service_auth_client_secret: SecretStr

    # Browser admin login uses the same IDU auth helper as IDU_DVD.
    admin_role: str = "ADMIN"
    auth_helper_url: str | None = None
    auth_helper_api_key: SecretStr | None = None
    auth_helper_timeout: float = 15.0

    # --- Neo4j (graph store: documents, clauses, restrictions, entities, kinds) ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "normgraph"
    neo4j_database: str = "neo4j"
    # Names of the native vector indexes provisioned at startup.
    restriction_vector_index: str = "restriction_embedding"
    clause_vector_index: str = "clause_embedding"
    entity_vector_index: str = "entity_embedding"
    kind_vector_index: str = "kind_embedding"

    # --- IDU_DVD (source of documents, clauses and references) ---
    dvd_base_url: str = "http://localhost:8100"
    dvd_timeout: float = 120.0
    # When the graph lacks coverage, RAG search may fall back to IDU_DVD /search.
    dvd_search_fallback: bool = True

    # --- LLM provider (restriction extraction via langextract + aux tasks) ---
    # "openai_compatible" — any OpenAI-compatible /v1 endpoint (vLLM, LM Studio,
    # llama.cpp, Ollama /v1, ...); "ollama" — native Ollama /api endpoint.
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_api_key: str | None = None
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    # Ceiling of the output window: an answer cut at llm_max_tokens is requested again with a
    # doubled budget up to this value. Set it equal to llm_max_tokens to disable the growth.
    llm_max_tokens_limit: int = 16384
    llm_timeout: float = 600.0
    # Reasoning budget for reasoning models on OpenAI-compatible endpoints (gpt-oss on vLLM):
    # "low" | "medium" | "high"; empty = not sent. With the model default, gpt-oss sometimes
    # spends the whole llm_max_tokens on reasoning and returns empty content.
    llm_reasoning_effort: Literal["low", "medium", "high"] | None = None
    # Native-Ollama fallback endpoint (used only when llm_provider == "ollama").
    ollama_base: str = "http://localhost:11434"

    # --- Embeddings provider (vectorizer) ---
    # "openai_compatible" — Giga-Embeddings-instruct via OpenAI-compatible /v1/embeddings
    # (2048-d, the default, matches the current IDU_DVD vector space);
    # "ollama" — native Ollama /api/embed (e.g. bge-m3, 1024-d).
    embeddings_provider: str = "openai_compatible"
    embeddings_url: str = "http://localhost:8001"
    embeddings_model: str = "ai-sage/Giga-Embeddings-instruct"
    embeddings_api_key: str | None = None
    # Instruction prefix for query embeddings (the Giga model is asymmetric: documents are
    # embedded without a prompt, queries with one).
    embeddings_query_prompt: str = (
        "Instruct: Дан вопрос, необходимо найти ограничение с ответом\nQuery: "
    )
    embeddings_timeout: float = 600.0
    # Vector dimension — must match the active embeddings model (giga = 2048, bge-m3 = 1024).
    vector_size: int = 2048
    embed_batch: int = 32

    # --- Extraction pipeline (langextract restriction extraction) ---
    # Number of langextract sequential extraction passes over each clause (recall vs cost).
    extraction_passes: int = 1
    # Cosine-similarity threshold for merging an extracted entity into an existing
    # canonical :Entity across documents (dedup). Below it a new entity node is created.
    entity_merge_threshold: float = 0.90
    # Query-time threshold for resolving a user's object (``restrictions_applicable``) to
    # canonical entities. Merging must be strict, lookup must not: with Giga embeddings
    # "детские сады" ~ "детский сад" is 0.87 and "школы" ~ "общеобразовательные организации"
    # 0.80, while unrelated facilities ("школы" ~ "детские сады") stay below 0.68.
    entity_query_threshold: float = 0.75
    # Cosine-similarity threshold for matching an extracted restriction kind to an
    # existing :RestrictionKind. Below it a new kind is created with status="pending".
    kind_match_threshold: float = 0.88
    # Max clauses processed concurrently through the LLM (GPU is the bottleneck).
    extract_concurrency: int = 64

    # --- CheckPlan planner (see src/pipeline/check_plan_planner.py) ---
    # Urban API root holding the public type dictionaries (``.../api``): plan entities
    # are grounded in /v1/service_types, /v1/physical_object_types and
    # /v1/functional_zones_types. Unset = no grounding and no LLM rewrite pass.
    urban_api_url: str | None = None
    urban_catalog_ttl_seconds: float = 3600.0
    # LLM pass that re-reads a norm without a grounded plan from its clause.
    check_plan_rewrite: bool = True
    # Independent rewrites that must agree (temperatures 0.0, 0.7, 0.4, ...).
    check_plan_rewrite_votes: int = 2
    # LLM verifier that must confirm every automatic plan.
    check_plan_verify: bool = True
    # Distances below this are in-building (furniture, equipment), not territorial.
    check_plan_min_distance_m: float = 3.0
    # Concurrent planner LLM requests (rewrite + verify).
    check_plan_llm_concurrency: int = 16

    # --- Search / graph traversal ---
    search_limit: int = 10
    max_traversal_depth: int = 3  # cap on graph-neighbourhood expansion depth

    # --- Kafka (incremental sync from IDU_DVD document.events via otteroad) ---
    # Consumption stays off until a broker is configured (empty/None = disabled).
    kafka_bootstrap_servers: str | None = None  # e.g. "kafka:9092"; None = disabled
    kafka_schema_registry_url: str = (
        "https://schema-registry.next.idulab.ru"  # AVRO Schema Registry (IDU contour)
    )
    kafka_client_id: str = "normgraph"
    kafka_group_id: str = "normgraph-sync"
    kafka_topic: str = "document.events"
    # Offset policy for a brand-new consumer group ("earliest" replays the backlog once).
    kafka_auto_offset_reset: str = "earliest"

    # --- Startup reconcile (catch documents missed while the consumer was down) ---
    reconcile_on_startup: bool = True
    # Generate missing plans from stored restrictions independently of document sync.
    check_plan_backfill_on_startup: bool = True
    # Recompute restriction vectors stored with an older embedding text (no LLM calls).
    restriction_reembed_on_startup: bool = True

    # --- Logging ---
    log_dir: str = "./logs"
    log_file: str = "app.log"
    log_level: str = "INFO"

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def _empty_reasoning_effort_is_unset(cls, value):
        return value or None

    @model_validator(mode="after")
    def _enforce_llm_endpoint_policy(self) -> "Settings":
        """Keep generative LLM traffic off a.dgx and remote native Ollama hosts."""
        if self.llm_provider == "ollama":
            host = _url_host(self.ollama_base, "NG_OLLAMA_BASE")
            if host not in _LOCAL_OLLAMA_HOSTS:
                raise ValueError(
                    "NG_OLLAMA_BASE must point to local Ollama when "
                    f"NG_LLM_PROVIDER=ollama; host {host!r} is not allowed"
                )
        elif _url_host(self.llm_base_url, "NG_LLM_BASE_URL") == "a.dgx":
            raise ValueError(
                "NG_LLM_BASE_URL must not target 'a.dgx' for language-model requests"
            )
        return self

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"neo4j={self.neo4j_uri} db={self.neo4j_database}, "
            f"dvd={self.dvd_base_url}, "
            f"llm={self.llm_provider} model={self.llm_model}, "
            f"embeddings={self.embeddings_provider} model={self.embeddings_model} "
            f"dim={self.vector_size}, "
            f"kafka={'on' if self.kafka_bootstrap_servers else 'off'})"
        )


settings = Settings()

# Архитектура

NormGraph — сервис на FastAPI + FastMCP, который превращает корпус документов IDU_DVD в **граф
нормативных ограничений** в Neo4j и отдаёт его по HTTP и MCP.

## Компоненты

```text
                 ┌──────────────────────────── NormGraph ────────────────────────────┐
                 │                                                                    │
 IDU_DVD ───────▶│  dvd_client ──▶ ingestion ──▶ graph.writer ─┐                      │
 /library,/search│                                             ├──▶  Neo4j            │
 document.events▶│  sync.consumer ──▶ sync.service ──▶ pipeline ┘   (граф + векторы)  │
 (Kafka)         │                         │                                          │
                 │  providers (LLM+embed) ◀┘                          graph.reader ──▶│──▶ query ──▶ REST + MCP
                 └────────────────────────────────────────────────────────────────────┘
     LLM (OpenAI-совместимый / Ollama)          эмбеддинги (Giga-Embeddings-instruct, 2048-мерные)
```

| Пакет | Ответственность |
|---|---|
| `src/dvd_client` | Асинхронный клиент IDU_DVD (`/library`, `/search`, lookup) + DTO. |
| `src/graph` | Клиент Neo4j, схема (constraints + векторные индексы), `writer` (идемпотентная запись), `reader` (поиск/обход/фасеты). |
| `src/ingestion` | Структурный слой: тянет документ, upsert `:Document` / `:Clause`, строит `PART_OF` и `REFERENCES`. |
| `src/pipeline` | Извлечение ограничений: промпт/примеры langextract, экстрактор, словарь видов, резолвер сущностей, оркестрация. |
| `src/providers` | Провайдер-независимые интерфейсы LLM + эмбеддингов, реализации OpenAI-совместимая и Ollama, бэкенд langextract. |
| `src/query` | Оркестрация чтения: эмбеддинг запроса → чтение графа → DTO ответа (search, applicable, get, graph, фасеты). |
| `src/sync` | Жизненный цикл: Kafka-консюмер (otteroad), сервис синхронизации (ingest+extract / delete / reconcile), guard идемпотентности. |
| `src/dto` | Модели запросов/ответов query-API. |
| `src/mcp_server` | FastMCP-сервер на `/mcp`, зеркалит query-API. |
| `src/system_service` | Health, логи, эффективные настройки. |

Всё связывается один раз в `src/dependencies.py` (composition root) и живёт через FastAPI lifespan.

## Модель графа

### Узлы

| Метка | Ключ | Основные свойства |
|---|---|---|
| `:Document` | `doc_id` | `name`, `version`, `version_id`, `content_hash`, `doc_type`, `corpus`, `lang` |
| `:Clause` | `node_id` | `doc_id`, `version`, `version_id`, `numbering`, `breadcrumb`, `type`, `depth`, `order`, `char_start/end`, `span_id`, `tags`, `text`, `embedding` |
| `:Restriction` | `id` | `subject`, `object`, `kind`, `kind_status`, `value_operator/number/unit/condition`, `doc_id`, `version_id`, `clause_node_id`, `extraction_text`, `char_start/end`, `embedding` |
| `:Entity` | `normalized` | `name`, `aliases`, `status`, `embedding` |
| `:RestrictionKind` | `name` | `status` (`approved`/`pending`), `aliases`, `embedding` |
| `:PendingReference` | `key` | `target_name`, `target_numbering` (цель ссылки, ещё не загруженная в граф) |

### Рёбра

| Ребро | Откуда → Куда | Смысл |
|---|---|---|
| `IN_DOCUMENT` | Clause → Document | принадлежность пункта документу |
| `PART_OF` | Clause → Clause | структурная иерархия (родитель) |
| `REFERENCES` | Clause → Clause / Document / PendingReference | ссылка (свойства: `scope`, `resolved`, `raw`, `target_numbering`) |
| `DERIVED_FROM` | Restriction → Clause | провенанс: из какого пункта извлечено ограничение |
| `HAS_SUBJECT` | Restriction → Entity | сущность, которая накладывает ограничение |
| `APPLIES_TO` | Restriction → Entity | сущность, на которую накладывается |
| `OF_KIND` | Restriction → RestrictionKind | вид из контролируемого словаря |
| `SHARES_ENTITY` | Restriction — Restriction | ненаправленное: два ограничения делят сущность subject/object |

### Граф связей ограничений

Два ограничения «связаны», если:

1. **`SHARES_ENTITY`** — делят сущность subject или object (семантическая связь); или
2. **ссылка** — их пункты соединены графом `REFERENCES` документов
   (`Restriction → Clause -[:REFERENCES]- Clause ← Restriction`).

`neighbors`/`traverse` раскрывают оба типа. Поскольку сущности дедуплицируются между документами
(см. [pipeline](pipeline.md)), одна и та же сущность, записанная по-разному в разных документах,
схлопывается в один `:Entity` — именно это связывает ограничения из разных документов.

### Векторные индексы

При старте создаются четыре нативных векторных индекса Neo4j (cosine, размерность = `NG_VECTOR_SIZE`,
по умолчанию 2048): `restriction_embedding`, `clause_embedding`, `entity_embedding`,
`kind_embedding`. Они обеспечивают семантический поиск по ограничениям и подбор по эмбеддинг-близости
при дедупе сущностей и резолве видов.

## Что переиспользуется из IDU_DVD

IDU_DVD уже разбирает документы на пункты с иерархией, тегами, привязкой к источнику
(`char_start/end`, `span_id`) и **разрешёнными ссылками** (`target_doc_id`, `target_node_id`,
`resolved`). NormGraph потребляет это через read-API `/library/documents/{doc_id}` (который отдаёт
`references` — небольшое расширение DTO IDU_DVD, на которое опирается NormGraph) и строит граф
`REFERENCES` напрямую, без повторного парсинга. Если по текстовому запросу граф пуст, поиск может
опереться на `/search` IDU_DVD и вернуть текстовые фрагменты-первоисточники.

## Рантайм и конвенции

- FastAPI на порту **8020**; MCP на `/mcp`; Swagger на `/docs`.
- Python 3.13+, пакеты через **uv**; `black` + `isort` через pre-commit; JSON-логи structlog.
- Подключается к Docker-сети **`localnet`**; Neo4j поднимается через `docker compose`.
- Аутентификации пока нет — держать в доверенной сети.

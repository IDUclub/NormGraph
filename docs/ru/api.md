# API

Базовый URL `http://localhost:8020`. Интерактивная документация (Swagger) на `/docs`; MCP на `/mcp`.
Все модели — pydantic; DTO запросов/ответов в `src/dto/query.py`. API без аутентификации — держать в
доверенной сети.

## Список эндпоинтов

| Метод и путь | Назначение |
|---|---|
| `POST /restrictions/search` | поиск ограничений по тексту и/или фильтрам |
| `POST /restrictions/applicable` | ограничения, применимые к заданному объекту/сущности |
| `GET /restrictions/{id}` | одно ограничение + провенанс + прямые соседи |
| `GET /restrictions/{id}/graph` | обход графа ограничений |
| `GET /entities` | канонические сущности (фасеты) |
| `GET /restriction-kinds` | словарь видов ограничений |
| `POST /ingestion/documents/{doc_id}` | структурный ингест одного документа |
| `POST /ingestion/by-name` | структурный ингест по имени документа |
| `GET /ingestion/stats` | счётчики узлов/рёбер |
| `POST /extraction/documents/{doc_id}` | извлечь ограничения из загруженного документа |
| `POST /sync/documents/{doc_id}` | ингест + извлечение одного документа (идемпотентно) |
| `POST /sync/by-name` | ингест + извлечение по имени |
| `POST /sync/reconcile` | принудительный полный reconcile |
| `DELETE /sync/by-name` | удалить документ (все версии) из графа |
| `GET /sync/status` | состояние Kafka-консюмера и настроек синхронизации |
| `GET /system/health` | готовность (пингует Neo4j) |
| `GET /system/settings` | эффективная конфигурация `NG_` (секреты замаскированы) |
| `GET /system/logs` | скачать JSON-лог |
| `GET /ping` | liveness |

## Основные формы

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

`value` = `null`, если у ограничения нет количественного параметра. `score` заполняется только для
векторного (текстового) поиска.

## POST /restrictions/search

Поиск ограничений. Тело (`RestrictionSearchRequest`):

| Поле | Тип | По умолч. | Описание |
|---|---|---|---|
| `query` | str? | null | текстовый запрос; без него → фильтрованный листинг (без вектора) |
| `kind` | str? | null | фильтр по виду |
| `doc_id` | str? | null | фильтр по документу |
| `document_names` | list[str]? | null | по любому из имён документов |
| `version` | str? | null | по версии или `version_id` |
| `doc_type` / `corpus` / `lang` | str? | null | фильтры классификации документа |
| `tags` | list[str]? | null | по тегам пункта (любой из) |
| `subject` / `object` | str? | null | по сущности subject/object (нормализованное/алиас) |
| `limit` | int | 10 | максимум хитов |
| `neighbors_depth` | int | 0 | также вернуть окрестность графа до этой глубины |

Ответ (`SearchResponse`): `{ count, hits: [RestrictionOut], neighbors: [{relation, restriction}], dvd_fallback: [DVDHit] }`.
`dvd_fallback` заполняется, только если текстовый запрос не дал ограничений и включён
`NG_DVD_SEARCH_FALLBACK` — в нём сырые фрагменты-первоисточники из IDU_DVD.

```bash
curl -X POST http://localhost:8020/restrictions/search \
     -H "Content-Type: application/json" \
     -d '{"query": "санитарно-защитная зона", "tags": ["зонирование"], "limit": 5}'
```

## POST /restrictions/applicable

Какие ограничения применимы к заданному объекту/сущности (сценарий проверки соответствия). Тело
(`ApplicableRequest`): те же фильтры, что и в поиске, плюс обязательный `object` (проверяемая
сущность), опциональные `subject`, `limit` (по умолч. 20). Объект резолвится в канонические сущности
(точное совпадение + ближайшие по эмбеддингу ≥ `NG_ENTITY_MERGE_THRESHOLD`), и возвращаются
ограничения, `APPLIES_TO` этих сущностей. Ответ — `SearchResponse`.

```bash
curl -X POST http://localhost:8020/restrictions/applicable \
     -H "Content-Type: application/json" -d '{"object": "жилая застройка", "limit": 10}'
```

## GET /restrictions/{id}

Одно ограничение как `RestrictionDetail` = `RestrictionOut` + `neighbors: [{relation, restriction}]`
(прямые соседи). `relation` ∈ `shares_entity` | `reference`. `404`, если не найдено.

## GET /restrictions/{id}/graph?depth=N

Обход графа ограничений от заданного до `depth` шагов (ограничено `NG_MAX_TRAVERSAL_DEPTH`). Ответ
(`GraphResponse`):

```json
{
  "root_id": "r1", "depth": 2,
  "nodes": [ RestrictionOut, ... ],
  "edges": [ {"source": "r1", "target": "r2", "relation": "shares_entity"}, ... ]
}
```

## GET /entities  ·  GET /restriction-kinds

Фасеты. `GET /entities?query=<подстрока>&limit=<n>` → `[{normalized, name, aliases, status,
restriction_count}]`, сначала наиболее упоминаемые. `GET /restriction-kinds` → `[{name, status,
aliases, restriction_count}]`, включая авто-добавленные виды `pending`.

## Ингест и извлечение

- `POST /ingestion/documents/{doc_id}` → `IngestResult` `{doc_id, clauses, references,
  pending_references, pruned_clauses, content_hash, skipped, reason}`. Только структура (без LLM).
- `POST /ingestion/by-name?name=<имя>` → `[IngestResult]`.
- `GET /ingestion/stats` → `{documents, clauses, references, pending_references, restrictions}`.
- `POST /extraction/documents/{doc_id}` → `ExtractResult` `{doc_id, clauses_processed, restrictions,
  pending_kinds, replaced, skipped, reason}`. Нужны LLM + эмбеддер.

## Синхронизация

- `POST /sync/documents/{doc_id}?replace=false` → `SyncResult` `{doc_id, name, clauses, restrictions,
  pruned_clauses, replaced, extraction_skipped, skipped, reason}`. Ингест **и** извлечение, с guard'ом
  идемпотентности (`extraction_skipped=true`, если не изменился и уже извлечён). `404`, если документа
  нет в DVD.
- `POST /sync/by-name?name=<имя>&replace=false` → `[SyncResult]`.
- `POST /sync/reconcile` → `ReconcileResult` `{added, updated, deleted, unchanged, failed, skipped,
  reason}`.
- `DELETE /sync/by-name?name=<имя>` → `DeleteResult` `{name, documents_deleted, clauses_deleted,
  restrictions_deleted, doc_ids}`.
- `GET /sync/status` → `{kafka_enabled, kafka_topic, kafka_group_id, kafka_bootstrap_servers,
  reconcile_on_startup}`.

## Система

- `GET /system/health` → `{status, graph}` (пингует Neo4j).
- `GET /system/settings` → эффективная конфигурация `NG_`; секреты (`neo4j_password`, `llm_api_key`,
  `embeddings_api_key`) замаскированы как `***`.
- `GET /system/logs` → JSON-лог.

## MCP-инструменты (`/mcp`)

FastMCP-сервер зеркалит query-API, чтобы gMART мог обращаться к ограничениям по MCP.

| Инструмент | Описание |
|---|---|
| `search_restrictions` | поиск по тексту/фильтрам; параметры как у `POST /restrictions/search` |
| `restrictions_applicable` | ограничения, применимые к `object` (+ опц. фильтры) |
| `get_restriction` | одно ограничение + провенанс + соседи |
| `traverse_restrictions` | обход графа от ограничения (`depth`) |
| `list_entities` | фасеты сущностей |
| `list_restriction_kinds` | словарь видов |
| `health` | liveness MCP-сервера |

Пример (in-memory клиент FastMCP):

```python
from fastmcp import Client
from src.mcp_server.server import mcp

async with Client(mcp) as client:
    res = await client.call_tool("search_restrictions", {"kind": "запрет_размещения", "limit": 5})
    print(res.structured_content)
```

# API

Базовый URL `http://localhost:8020`. Интерактивная документация (Swagger) на `/docs`; MCP на `/mcp`.
Все модели — pydantic; DTO запросов/ответов в `src/dto/query.py`. API без аутентификации — держать в
доверенной сети.

## Список эндпоинтов

| Метод и путь | Назначение |
|---|---|
| `POST /restrictions/search` | поиск ограничений по тексту и/или фильтрам |
| `POST /restrictions/applicable` | ограничения, применимые к заданному объекту/сущности |
| `POST /restrictions/list` | полный постраничный листинг для аудита |
| `GET /restrictions/{id}` | одно ограничение + провенанс + прямые соседи |
| `GET /restrictions/{id}/graph` | обход графа ограничений |
| `GET /check-plans/review` | очередь auto/pending планов для экспертного ревью |
| `POST /check-plans/backfill` | создать ограниченный батч отсутствующих планов без re-extraction |
| `GET /check-plans/{id}/revisions` | неизменяемая история CheckPlan нормы |
| `POST /check-plans/{id}/review` | approve, reject или replace плана |
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

`RestrictionOut` дополнительно содержит опциональный `check_plan` и метаданные
`check_plan_revision`, `check_plan_review_status`. Поле отсутствует/равно `null` у
старых записей и не требует массовой миграции. Поиск, applicable и get возвращают
один и тот же текущий план; автор и время доступны в элементах истории ревизий.

## Ревью CheckPlan

`GET /check-plans/review?limit=50` возвращает планы, ожидающие экспертного решения.
`GET /check-plans/{restriction_id}/revisions` возвращает неизменяемую историю
ревизий.

```http
POST /check-plans/{restriction_id}/review
Content-Type: application/json

{
  "action": "approve",
  "reason": "План проверен по первоисточнику"
}
```

`action` принимает `approve`, `reject` или `replace`. Для `replace` обязателен
полный валидный `plan`. Автор берётся из проверенной идентичности запроса. Каждое
действие создаёт новую ревизию; reviewed-план защищён от автоматической перезаписи.

## POST /check-plans/backfill

Генерирует планы непосредственно из сохранённых ограничений, у которых нет текущего `CheckPlan`.
Операция не удаляет ограничения, использует keyset-пагинацию и не запускает повторное извлечение
пунктов документа.

```json
{"limit": 100, "after_id": null, "dry_run": false}
```

Ответ содержит `selected`, `generated`, `auto`, `unsupported`, `skipped`, `failed`, отдельные
`failures` и поля пагинации `has_more`/`next_after_id`. Пока `has_more=true`, передавайте
`next_after_id` как `after_id` следующего запроса. Dry-run только читает батч. Повторный запуск с
`after_id=null` безопасен и повторяет строки, ранее завершившиеся ошибкой; ограничения с текущим
планом атомарно пропускаются.

## POST /restrictions/search

Поиск ограничений. Тело (`RestrictionSearchRequest`):

| Поле | Тип | По умолч. | Описание |
|---|---|---|---|
| `query` | str? | null | текстовый запрос; без него → фильтрованный листинг (без вектора) |
| `kind` | str? | null | фильтр по виду |
| `kinds` | list[str]? | null | любой из этих видов (например, все виды размещения) |
| `doc_id` | str? | null | фильтр по документу |
| `document_names` | list[str]? | null | по любому из имён документов |
| `version` | str? | null | по версии или `version_id` |
| `doc_type` / `corpus` / `lang` | str? | null | фильтры классификации документа |
| `tags` | list[str]? | null | по тегам пункта (любой из) |
| `subject` / `object` | str? | null | по сущности subject/object (нормализованное/алиас) |
| `limit` | int | 10 | максимум хитов, 1–500 |
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
сущность), опциональные `subject`, `limit` (по умолч. 20, не больше 500). Объект резолвится в канонические сущности
(точное совпадение + ближайшие по эмбеддингу ≥ `NG_ENTITY_QUERY_THRESHOLD`, мягче порога слияния), и возвращаются
ограничения, `APPLIES_TO` этих сущностей. Ответ — `SearchResponse`.

```bash
curl -X POST http://localhost:8020/restrictions/applicable \
     -H "Content-Type: application/json" -d '{"object": "жилая застройка", "limit": 10}'
```

## POST /restrictions/list

Полный листинг для аудита (так весь корпус читает проверка соответствия в gMART). Один ответ — не
больше 500 ограничений: окно шире исчерпывает память сервера, поэтому search и applicable такие
запросы тоже отклоняют. Тело (`RestrictionListRequest`): фильтры поиска плюс `after_id` (null для
первой страницы), `limit` (по умолч. 200, 1–500) и `executable_only` (только ограничения, у которых
текущий CheckPlan `auto` или `reviewed`). Страницы упорядочены по id ограничения, поэтому документы,
загруженные во время обхода, не сдвигают и не дублируют строки. Ответ (`RestrictionPage`):
`{ count, hits: [RestrictionOut], next_after_id }`; повторяйте с `after_id = next_after_id`, пока он не
станет null.

```bash
curl -X POST http://localhost:8020/restrictions/list \
     -H "Content-Type: application/json" -d '{"limit": 200, "executable_only": true}'
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

## POST /check-plans/{id}/regenerate

Пересоздаёт план по сохранённой норме без повторного извлечения документа. Требуется сервисный
Bearer-токен. Текущую ревизию возьмите из `GET /check-plans/{id}/revisions`; если плана ещё нет,
используйте `0`. По умолчанию выполняется предпросмотр без записи:

```json
{"expected_revision": 1, "dry_run": true}
```

Ответ содержит `restriction_id`, `revision`, `dry_run` и `plan`. Для сохранения передайте
`dry_run=false` и ту же ожидаемую ревизию. Создаётся новая текущая ревизия, история сохраняется.
При сохранении планировщик запускается снова: результат LLM может отличаться от предпросмотра.
В предпросмотре `revision` — существующая ревизия, при записи — новая.
`404` означает отсутствие нормы; `409` — изменение ревизии или защищённое решение эксперта.
Планы со статусом `reviewed` либо автором экспертного решения, включая отклонённые, не заменяются.
Некорректный запрос возвращает `422`.

### Доступность и условия применимости

Для распознанной нормы доступности образовательных организаций проверяемый слой — `Жилой дом`.
Если названы оба вида организаций, `Школа` и `Детский сад` становятся отдельными обязательными
слоями: школа не заменяет детсад. Километры переводятся в метры; текст нормы и условия сохраняются.

Если указано только расстояние, без явного требования пешеходной или транспортной доступности
либо маршрута и без дополнительных условий, формируется исполняемый план `presence_within`
со статусом `auto`. Например, базовое ограничение «не более 500 м» проверяется по геометрическому
расстоянию. Само упоминание школы или детсада не означает требование пешеходного маршрута.

Исполнитель v1 рассчитывает геометрические буферы и не проверяет пешеходные маршруты или условия
применимости. Поэтому такие планы получают `template=unsupported`, `planner_status=unsupported`
и должны давать «не проверено», а не заключение о соответствии по расстоянию по прямой.
В `params.blocked_reasons` записываются причины `walking_route_required` и/или
`applicability_not_verified`, в `params.condition` — исходное условие. Если возможно построить
геометрический черновик, он сохраняется в `params.candidate_plan` **только для просмотра**.
Его нельзя исполнять отдельно или одобрять, пока эти ограничения не устранены. Черновик с лимитом
1 км для сельской местности не подтверждает применимость этого лимита к городскому сценарию.

Защита охватывает явные упоминания пешеходной/транспортной доступности или маршрутов
и непустые извлечённые условия. Это не универсальная проверка смысла любой нормы.
Существующие планы изменятся только после развёртывания и явного пересоздания; backfill пропускает
нормы, у которых уже есть план.

## Ингест и извлечение

- `POST /ingestion/documents/{doc_id}` → `IngestResult` `{doc_id, clauses, references,
  pending_references, pruned_clauses, content_hash, skipped, reason}`. Только структура (без LLM).
- `POST /ingestion/by-name?name=<имя>` → `[IngestResult]`.
- `GET /ingestion/stats` → `{documents, clauses, references, pending_references, restrictions}`.
- `POST /extraction/documents/{doc_id}` → `ExtractResult` `{doc_id, clauses_processed, restrictions,
  pending_kinds, conflicts, replaced, skipped, reason, warnings, incomplete, failed_clause_ids}`. Нужны LLM + эмбеддер.

### POST /extraction/backfill

Повторное извлечение из уже загруженных документов, у которых **нет ни одного ограничения**.
Использует пункты из графа, без повторного скачивания документов. При извлечении автоматически
создаются CheckPlans. Требуется тот же сервисный Bearer-токен, что и для остальных эндпоинтов извлечения.

```json
{"limit": 1, "after_id": null, "dry_run": true}
```

`limit` — число документов (1–20, по умолчанию 1). `dry_run=true` возвращает список ID без вызовов
LLM и записи в граф; для извлечения передайте `false`. Запрос ждёт завершения порции. Документы
обрабатываются последовательно, параллелизм пунктов задаётся `NG_EXTRACT_CONCURRENCY`. HTTP-тайм-аут
клиента должен учитывать длительность извлечения целого документа.

Ответ: `selected`, `extracted`, `skipped`, `failed`, `restrictions`, список `items` с результатами
по документам (статус, счётчики пунктов/ограничений, причина), `has_more`, `next_after_id`, `dry_run`.
При `has_more=true` передайте `next_after_id` в `after_id` следующего запроса. Ошибка одного документа
не останавливает порцию. Успешное извлечение может дать ноль ограничений: курсор продвинется дальше,
но при новом обходе такой документ снова попадёт в выборку.

Документы с существующими ограничениями исключаются, ограничения не удаляются. Частично извлечённые
документы восстанавливаются через `POST /extraction/documents/{doc_id}` — в том числе после ошибки,
если часть ограничений уже записалась. Новый обход без `after_id` повторяет документы, в которых
ограничений всё ещё нет. Не запускайте пересекающиеся извлечения/синхронизации для одних документов:
повторная проверка состояния не является распределённой блокировкой.

## Синхронизация

- `POST /sync/documents/{doc_id}?replace=false` → `SyncResult` `{doc_id, name, clauses, restrictions,
  pruned_clauses, replaced, extraction_skipped, skipped, reason, extraction_incomplete, warnings, failed_clause_ids}`. Ингест **и** извлечение, с guard'ом
  идемпотентности (`extraction_skipped=true`, если не изменился и уже извлечён). `404`, если документа
  нет в DVD.
- `POST /sync/by-name?name=<имя>&replace=false` → `[SyncResult]`.
- `POST /sync/reconcile` → `ReconcileResult` `{added, updated, relabelled, deleted, unchanged, failed,
  skipped, reason}`.
- `DELETE /sync/by-name?name=<имя>` → `DeleteResult` `{name, documents_deleted, clauses_deleted,
  restrictions_deleted, doc_ids}`.
- `GET /sync/status` → `{kafka_enabled, kafka_topic, kafka_group_id, kafka_bootstrap_servers,
  reconcile_on_startup}`.

## Система

`GET /system/logs` и `GET /system/settings` доступны без авторизации. `GET /system/health` требует сервисный токен.

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
| `list_restrictions` | полный постраничный листинг; параметры как у `POST /restrictions/list` |
| `get_restriction` | одно ограничение + провенанс + соседи |
| `traverse_restrictions` | обход графа от ограничения (`depth`) |
| `list_entities` | фасеты сущностей |
| `list_restriction_kinds` | словарь видов |
| `pending_check_plans` | очередь планов для ревью |
| `review_check_plan` | approve/reject/replace с новой ревизией |
| `health` | liveness MCP-сервера |

Пример (in-memory клиент FastMCP):

```python
from fastmcp import Client
from src.mcp_server.server import mcp

async with Client(mcp) as client:
    res = await client.call_tool("search_restrictions", {"kind": "запрет_размещения", "limit": 5})
    print(res.structured_content)
```
